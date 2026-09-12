"""LLM page configuration persistence and API contracts."""

import os
import stat
import sys
import types

import pytest

from codeevolution.api import (
    LLMConfigRequest,
    _request_dependencies,
    delete_llm_settings,
    get_llm_settings,
    save_llm_settings,
)
from codeevolution.infrastructure.llm_config_store import LLMConfigStore
from codeevolution.semantic.client import OpenAILLMClient
from codeevolution.semantic.config import get_llm_config


@pytest.fixture(autouse=True)
def no_environment_llm(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEEVOLUTION_LLM_MODEL",
        "CODEEVOLUTION_LLM_BASE",
        "CODEHISTORY_LLM_MODEL",
        "CODEHISTORY_LLM_BASE",
        "CODEEVOLUTION_LLM_DISABLE_SSL",
        "CODEHISTORY_LLM_DISABLE_SSL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_store_round_trip_is_private_and_atomic(tmp_path):
    path = tmp_path / "llm-config.json"
    store = LLMConfigStore(path)
    expected = {
        "api_key": "secret",
        "model": "openai/test",
        "api_base": "https://llm.test/v1",
        "disable_ssl_verification": True,
    }
    assert store.save(expected) == expected
    assert store.load() == expected
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.delete()
    assert store.load() is None


def test_environment_configuration_overrides_page_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEEVOLUTION_DATA_DIR", str(tmp_path))
    LLMConfigStore().save({"api_key": "page", "model": "page-model", "api_base": ""})
    monkeypatch.setenv("OPENAI_API_KEY", "environment")
    monkeypatch.setenv("CODEEVOLUTION_LLM_MODEL", "environment-model")
    assert get_llm_config()["api_key"] == "environment"
    assert get_llm_config()["model"] == "environment-model"


def test_legacy_environment_configuration_remains_supported(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment")
    monkeypatch.setenv("CODEHISTORY_LLM_MODEL", "legacy-model")
    monkeypatch.setenv("CODEHISTORY_LLM_BASE", "https://legacy.test/v1")

    assert get_llm_config() == {
        "api_key": "environment",
        "model": "legacy-model",
        "api_base": "https://legacy.test/v1",
        "disable_ssl_verification": False,
    }


def test_api_never_returns_key_and_blank_update_retains_it(tmp_path):
    store = LLMConfigStore(tmp_path / "config.json")
    token = _request_dependencies.set({"llm_config_store": store})
    try:
        save_llm_settings(
            LLMConfigRequest(
                model="model-a", api_key="secret", disable_ssl_verification=True
            )
        )
        response = get_llm_settings()
        assert response["api_key_configured"] is True
        assert response["disable_ssl_verification"] is True
        assert "api_key" not in response

        save_llm_settings(LLMConfigRequest(model="model-b", api_key=""))
        assert store.load() == {
            "api_key": "secret",
            "model": "model-b",
            "api_base": "",
            "disable_ssl_verification": True,
        }
        assert delete_llm_settings() == {"ok": True, "deleted": True}
    finally:
        _request_dependencies.reset(token)


def test_client_supports_http_and_explicitly_disables_ssl_verification(monkeypatch):
    observed = {}

    class FakeHttpClient:
        def __init__(self, **kwargs):
            observed["http_client_kwargs"] = kwargs

        def close(self):
            observed["closed"] = True

    class FakeCompletions:
        def create(self, **kwargs):
            observed["request"] = kwargs
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="OK"))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            observed["client"] = kwargs
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=FakeHttpClient))

    result = OpenAILLMClient(
        {
            "api_key": "secret",
            "model": "local-model",
            "api_base": "http://localhost:8000/v1",
            "disable_ssl_verification": True,
            "disable_thinking": False,
        }
    ).complete("Reply with exactly: OK")

    assert result == "OK"
    assert observed["client"]["base_url"] == "http://localhost:8000/v1"
    assert observed["http_client_kwargs"] == {"verify": False}
    assert observed["closed"] is True
