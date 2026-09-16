import json
import sys

import pytest

from codeevolution import api, cli
from codeevolution.api import (
    ChatRequest,
    _request_dependencies,
    app,
    ask_repository,
    create_app,
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


def test_knowledge_route_can_request_complete_api_contract():
    class SnapshotQueries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "frozen-1"
            return {"endpoint_count": 2, "endpoints": [{"path": "/one"}, {"path": "/two"}]}

    class SnapshotStore:
        def get_llm_knowledge_job(self, snapshot_id):
            assert snapshot_id == "frozen-1"
            return None

    class Runtime:
        snapshot_queries = SnapshotQueries()
        store = SnapshotStore()

    isolated = create_app({"snapshot_runtime": Runtime()})
    token = _request_dependencies.set(isolated.state.dependencies)
    try:
        assert get_knowledge_report(
            snapshot_id="frozen-1", section="api", complete=True
        ) == {
            "api_contract": {
                "endpoint_count": 2,
                "endpoints": [{"path": "/one"}, {"path": "/two"}],
            }
        }
    finally:
        _request_dependencies.reset(token)


def test_cli_exports_one_contract_file_per_endpoint_without_source(monkeypatch, tmp_path):
    class Queries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "frozen-1"
            return {
                "endpoint_count": 2,
                "endpoints": [
                    {
                        "method": "GET",
                        "path": "/orders/{id}",
                        "request_headers": [{"name": "Authorization"}],
                        "request_body": None,
                        "path_params": [{"name": "id"}],
                        "query_params": [],
                        "response_body": {"type": "Order"},
                        "call_chain": [
                            {"id": "root", "name": "get", "source": "secret"},
                            {"id": "child", "name": "load", "aggregate_explanation": {"summary": "secret"}},
                        ],
                    },
                    {"method": "POST", "path": "/orders", "call_chain": []},
                ],
            }

    class Runtime:
        snapshot_queries = Queries()

        def __init__(self, _root):
            pass

        def close(self):
            pass

    output_dir = tmp_path / "contracts"
    monkeypatch.setattr(cli, "SnapshotRuntime", Runtime)
    monkeypatch.setattr(cli, "analysis_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codeevolution",
            "api-contract",
            "--snapshot-id",
            "frozen-1",
            "--format",
            "json",
            "--output-dir",
            str(output_dir),
        ],
    )

    cli.main()

    files = sorted(output_dir.glob("*.json"))
    assert len(files) == 2
    first = json.loads(files[0].read_text())
    assert set(first) == {
        "url", "method", "request_headers", "request_body", "path_params",
        "query_params", "response_body", "call_chain", "main_node_explanation",
    }
    assert "source" not in json.dumps(first, ensure_ascii=False)
    assert "aggregate_explanation" not in json.dumps(first["call_chain"], ensure_ascii=False)


def test_cli_exports_one_json_file_per_term_with_evidence(monkeypatch, tmp_path):
    terms = [
        {
            "id": "term-order",
            "canonical_name": "Order",
            "normalized_name": "order",
            "term_type": "entity",
            "bounded_context": "orders",
            "definition": "A purchase order.",
            "confidence_score": 0.95,
            "confidence_band": "high",
            "status": "accepted",
            "source": "rule",
            "risk_flags": [],
            "aliases": ["PurchaseOrder"],
        },
        {
            "id": "term-create-order",
            "canonical_name": "CreateOrder",
            "normalized_name": "createorder",
            "term_type": "action",
            "bounded_context": "orders",
            "definition": "Create an order.",
            "confidence_score": 0.9,
            "confidence_band": "high",
            "status": "accepted",
            "source": "rule",
            "risk_flags": [],
            "aliases": [],
        },
    ]

    class Service:
        def list(self, **query):
            assert query == {"snapshot_id": "frozen-1", "limit": 100, "offset": 0}
            return {"terms": terms, "total": len(terms), "limit": 100, "offset": 0}

        def evidence(self, snapshot_id, term_id):
            assert snapshot_id == "frozen-1"
            return [{
                "evidence_type": "entity_anchor",
                "evidence_value": term_id,
                "weight": 1.0,
                "source_location": {"file": "orders.py", "line": 12},
                "node_id": "node-order",
                "rule_id": "entity-anchor",
            }]

    class Resource:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_terms_local_context", lambda: (Resource(), Resource(), Service()))
    output_dir = tmp_path / "terms"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codeevolution",
            "terms",
            "export",
            "--snapshot-id",
            "frozen-1",
            "--format",
            "json",
            "--output-dir",
            str(output_dir),
        ],
    )

    cli.main()

    files = sorted(output_dir.glob("*.json"))
    assert len(files) == 2
    first = json.loads(files[0].read_text())
    assert first["canonical_name"] == "Order"
    assert first["snapshot_id"] == "frozen-1"
    assert first["evidence"][0]["source_location"] == {"file": "orders.py", "line": 12}


def test_cli_exports_terms_as_markdown(monkeypatch, tmp_path):
    class Service:
        def list(self, **_query):
            return {"terms": [{
                "id": "term-order", "canonical_name": "Order", "term_type": "entity",
                "definition": "A purchase order.", "aliases": [], "risk_flags": [],
            }]}

        def evidence(self, _snapshot_id, _term_id):
            return []

    class Resource:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_terms_local_context", lambda: (Resource(), Resource(), Service()))
    output_dir = tmp_path / "terms"
    monkeypatch.setattr(
        sys,
        "argv",
        ["codeevolution", "terms", "export", "--snapshot-id", "frozen-1", "--output-dir", str(output_dir)],
    )

    cli.main()

    content = next(output_dir.glob("*.md")).read_text()
    assert "# Order" in content
    assert "## Evidence" in content
    assert "_No evidence recorded._" in content


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
    monkeypatch.setattr(sys, "argv", ["codeevolution", "repos"])
    cli.main()
    assert called == ["repos"]


def test_cli_without_command_keeps_exit_contract(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codeevolution"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    assert "usage: codeevolution" in capsys.readouterr().out
