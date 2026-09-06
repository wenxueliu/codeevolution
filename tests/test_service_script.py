"""Regression tests for the Web lifecycle helper."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "codeevolution_service_script", Path(__file__).parents[1] / "scripts" / "service.py"
)
service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(service)

START_SPEC = importlib.util.spec_from_file_location(
    "codeevolution_start_script", Path(__file__).parents[1] / "start.py"
)
start_script = importlib.util.module_from_spec(START_SPEC)
assert START_SPEC.loader
START_SPEC.loader.exec_module(start_script)


def test_one_click_start_uses_default_address_and_builds(monkeypatch):
    calls = []
    monkeypatch.setattr(start_script.service, "start", lambda *args, **kwargs: calls.append((args, kwargs)))

    start_script.main([])

    assert calls == [(("127.0.0.1", 8765), {"should_build": True})]


def test_one_click_start_forwards_options(monkeypatch):
    calls = []
    monkeypatch.setattr(start_script.service, "start", lambda *args, **kwargs: calls.append((args, kwargs)))

    start_script.main(["--host", "0.0.0.0", "--port", "9000", "--no-build"])

    assert calls == [(("0.0.0.0", 9000), {"should_build": False})]


def test_start_rejects_an_occupied_port_before_build(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "PID_FILE", tmp_path / "service.pid")
    monkeypatch.setattr(service, "port_is_available", lambda _host, _port: False)
    built = []
    monkeypatch.setattr(service, "build", lambda: built.append(True))

    try:
        service.start("127.0.0.1", 8765)
    except RuntimeError as error:
        assert "already in use" in str(error)
    else:
        raise AssertionError("occupied port should fail")
    assert built == []


def test_api_readiness_requires_a_json_response(monkeypatch):
    class Headers:
        @staticmethod
        def get_content_type():
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(service.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert service.api_is_ready("0.0.0.0", 8765)


def test_legacy_pid_file_is_read_and_cleared(monkeypatch, tmp_path):
    current = tmp_path / "codeevolution.pid"
    legacy = tmp_path / "codehistory.pid"
    legacy.write_text("123", encoding="utf-8")
    monkeypatch.setattr(service, "PID_FILE", current)
    monkeypatch.setattr(service, "LEGACY_PID_FILE", legacy)

    assert service.read_pid() == 123
    service.clear_pid_files()
    assert not legacy.exists()
