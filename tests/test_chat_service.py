import sqlite3

import pytest

from codehistory.application.chat_service import ChatService
from codehistory.infrastructure.audit_store import AuditStore


class StoreStub:
    def get_stats(self):
        return {"total_features": 3}

    def query_features(self, **_options):
        return {"features": [{"canonical_name": "checkout"}]}

    def query_events(self, **_options):
        return {"events": [{"event_type": "GROWN"}]}


def make_graph(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE nodes (
             id TEXT, name TEXT, qualified_name TEXT, kind TEXT, file_path TEXT, start_line INTEGER
           );
           CREATE TABLE edges (source TEXT, target TEXT, kind TEXT);
           INSERT INTO nodes VALUES ('a', 'createOrder', 'OrderService.createOrder', 'method', 'order.py', 10);
           INSERT INTO nodes VALUES ('b', 'checkout', 'Checkout.checkout', 'method', 'checkout.py', 20);
           INSERT INTO edges VALUES ('b', 'a', 'calls');"""
    )
    connection.commit()
    connection.close()


def test_chat_executes_whitelisted_codegraph_plan_and_audits(tmp_path):
    repo = tmp_path / "repo"
    database = repo / ".codegraph" / "codegraph.db"
    database.parent.mkdir(parents=True)
    make_graph(database)
    audit = AuditStore(str(tmp_path / "audit.db"))

    class Planner:
        def complete(self, *_args, **_kwargs):
            return '[{"operation":"codegraph.find_callers","args":{"symbol":"createOrder"}}]'

    service = ChatService(
        audit,
        lambda _name: [{"name": "backend", "path": str(repo)}],
        lambda _name: StoreStub(),
        Planner(),
    )
    result = service.ask("mall", "谁调用了 createOrder？")
    assert result["operations"][0]["rows"][0]["name"] == "checkout"
    logs = audit.list("mall")
    assert logs[0]["status"] == "success"
    assert logs[0]["plan"][0]["operation"] == "codegraph.find_callers"


def test_chat_rejects_unknown_llm_operations_and_uses_safe_fallback(tmp_path):
    audit = AuditStore(str(tmp_path / "audit.db"))

    class UnsafePlanner:
        def complete(self, *_args, **_kwargs):
            return '[{"operation":"database.raw_sql","args":{"sql":"DROP TABLE features"}}]'

    service = ChatService(audit, lambda _name: [], lambda _name: StoreStub(), UnsafePlanner())
    result = service.ask("mall", "统计信息")
    assert result["operations"][0]["operation"] == "database.stats"
    assert result["operations"][0]["rows"] == [{"total_features": 3}]
    assert ChatService._heuristic_plan("谁调用了 createOrder？")[0]["args"]["symbol"] == "createOrder"


def test_failed_operation_is_also_audited(tmp_path):
    audit = AuditStore(str(tmp_path / "audit.db"))
    service = ChatService(audit, lambda _name: [], lambda _name: (_ for _ in ()).throw(ValueError("missing")))
    with pytest.raises(ValueError, match="missing"):
        service.ask("unknown", "统计")
    assert audit.list("unknown")[0]["status"] == "failed"
