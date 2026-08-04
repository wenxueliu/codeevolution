import sys

import pytest

from codehistory import api, cli
from codehistory.api import (
    ChatRequest,
    _request_dependencies,
    app,
    ask_repository,
    create_app,
    get_evolution_service,
    get_knowledge_report,
    list_assistant_audit_logs,
)


def test_chat_and_audit_routes_use_injected_services():
    class ChatStub:
        def ask(self, repo, question):
            return {"answer": question, "repo": repo}

    class AuditStub:
        def list(self, repo, limit):
            return [{"repository": repo, "limit": limit}]

    dependencies = {"chat_service": ChatStub(), "audit_store": AuditStub()}
    token = _request_dependencies.set(dependencies)
    try:
        assert ask_repository(ChatRequest(repo="mall", question="调用链")) == {
            "answer": "调用链",
            "repo": "mall",
        }
        assert list_assistant_audit_logs("mall", 10) == {
            "logs": [{"repository": "mall", "limit": 10}]
        }
    finally:
        _request_dependencies.reset(token)


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


def test_knowledge_route_uses_injected_service_without_closing_it():
    class FakeKnowledgeService:
        def __init__(self):
            self.closed = False

        def report(self, include_llm=False):
            return {"include_llm": include_llm, "api_contract": {"endpoint_count": 2}}

        def close(self):
            self.closed = True

    service = FakeKnowledgeService()
    isolated = create_app({"knowledge_service": service})
    token = _request_dependencies.set(isolated.state.dependencies)
    try:
        assert get_knowledge_report(repo="demo", include_llm=True) == {
            "include_llm": True,
            "api_contract": {"endpoint_count": 2},
        }
        assert service.closed is False
    finally:
        _request_dependencies.reset(token)


def test_unregister_route_removes_registration_and_closes_cached_store(monkeypatch):
    removed = []

    class FakeStore:
        closed = False

        def close(self):
            self.closed = True

    store = FakeStore()
    api._stores["orders"] = store
    monkeypatch.setattr(api, "get_repo", lambda name: {"name": name})
    monkeypatch.setattr(api, "unregister_repo", removed.append)
    try:
        assert api.api_unregister_repo("orders") == {
            "ok": True,
            "name": "orders",
            "deleted_data": False,
        }
        assert removed == ["orders"]
        assert store.closed is True
        assert "orders" not in api._stores
    finally:
        api._stores.pop("orders", None)


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
