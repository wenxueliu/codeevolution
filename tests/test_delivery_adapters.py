import sys

import pytest

from codehistory import cli
from codehistory.api import _request_dependencies, app, create_app, get_evolution_service


def test_create_app_preserves_routes_and_injects_configuration():
    isolated = create_app({"cors_origins": ["https://example.test"]})
    assert isolated is not app
    assert isolated.state.dependencies["cors_origins"] == ["https://example.test"]
    assert isolated.openapi()["paths"] == app.openapi()["paths"]


def test_create_app_injects_application_service_into_real_requests():
    service = type("Service", (), {"stats": lambda self: {"injected": True}})()
    isolated = create_app({"evolution_service": service})
    token = _request_dependencies.set(isolated.state.dependencies)
    try:
        assert get_evolution_service().stats() == {"injected": True}
    finally:
        _request_dependencies.reset(token)


def test_cli_dispatches_through_parser_handler(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "cmd_repos", lambda args: called.append(args.command))
    monkeypatch.setattr(sys, "argv", ["codehistory", "repos"])
    cli.main()
    assert called == ["repos"]


def test_cli_without_command_keeps_exit_contract(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codehistory"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    assert "usage: codehistory" in capsys.readouterr().out
