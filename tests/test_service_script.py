"""Regression tests for the Web lifecycle helper."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "codehistory_service_script", Path(__file__).parents[1] / "scripts" / "service.py"
)
service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(service)


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
