"""LLM page configuration persistence and API contracts."""

import os
import stat

import pytest

from codehistory.api import (
    LLMConfigRequest,
    _request_dependencies,
    delete_llm_settings,
    get_llm_settings,
    save_llm_settings,
)
from codehistory.infrastructure.llm_config_store import LLMConfigStore
from codehistory.semantic.config import get_llm_config


@pytest.fixture(autouse=True)
def no_environment_llm(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CODEHISTORY_LLM_MODEL",
        "CODEHISTORY_LLM_BASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_store_round_trip_is_private_and_atomic(tmp_path):
    path = tmp_path / "llm-config.json"
    store = LLMConfigStore(path)
    expected = {"api_key": "secret", "model": "openai/test", "api_base": "https://llm.test/v1"}
    assert store.save(expected) == expected
    assert store.load() == expected
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.delete()
    assert store.load() is None


def test_environment_configuration_overrides_page_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEHISTORY_DATA_DIR", str(tmp_path))
    LLMConfigStore().save({"api_key": "page", "model": "page-model", "api_base": ""})
    monkeypatch.setenv("OPENAI_API_KEY", "environment")
    monkeypatch.setenv("CODEHISTORY_LLM_MODEL", "environment-model")
    assert get_llm_config()["api_key"] == "environment"
    assert get_llm_config()["model"] == "environment-model"


def test_api_never_returns_key_and_blank_update_retains_it(tmp_path):
    store = LLMConfigStore(tmp_path / "config.json")
    token = _request_dependencies.set({"llm_config_store": store})
    try:
        save_llm_settings(LLMConfigRequest(model="model-a", api_key="secret"))
        response = get_llm_settings()
        assert response["api_key_configured"] is True
        assert "api_key" not in response

        save_llm_settings(LLMConfigRequest(model="model-b", api_key=""))
        assert store.load() == {"api_key": "secret", "model": "model-b", "api_base": ""}
        assert delete_llm_settings() == {"ok": True, "deleted": True}
    finally:
        _request_dependencies.reset(token)
