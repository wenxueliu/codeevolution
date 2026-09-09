import sqlite3
import subprocess
import json
from pathlib import Path

from codeevolution import cli, mcp_server
from codeevolution.application.analysis_run_service import RepositoryCatalogService
from codeevolution.application.repository_attempt_worker import RepositoryAttemptWorker
from codeevolution.application.snapshot_query_service import SnapshotQueryService, _project_api_node_ids
from codeevolution.application.chat_service import SnapshotChatService
from codeevolution.infrastructure.audit_store import AuditStore
from codeevolution.domain.analysis_snapshot import AttemptStatus
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore
from codeevolution.infrastructure.codegraph_command import CommandResult
from codeevolution.infrastructure.snapshot_bundle_resolver import SnapshotBundleResolver
from codeevolution.infrastructure.explanation_source import SnapshotExplanationSource


class _Runner:
    def init_or_sync(self, _root, _cancel):
        return CommandResult(("codegraph", "sync"), 0, "", "", 0.01)


def test_snapshot_knowledge_backfills_node_id_for_legacy_api_facts():
    facts = {"api_contract": {"endpoints": [{
        "handler": "orders::create",
        "call_chain": [{"id": "method:root", "name": "create"}],
    }]}}

    projected = _project_api_node_ids(facts)

    assert projected["api_contract"]["endpoints"][0]["node_id"] == "method:root"
    assert "node_id" not in facts["api_contract"]["endpoints"][0]


def _graph(path: Path) -> None:
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
          file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
          visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER, decorators TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
          metadata TEXT, line INTEGER, col INTEGER, provenance TEXT);
        CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, size INTEGER);
        INSERT INTO nodes VALUES ('root','function','root','root','app.py','python',1,2,'root()', '',1,0,0,'[]');
        INSERT INTO nodes VALUES ('child','function','child','child','app.py','python',4,5,'child()', '',1,0,0,'[]');
        INSERT INTO edges VALUES (1,'root','child','calls','{}',2,1,'');
        INSERT INTO files VALUES ('app.py','hash','python',42);
        """)


def test_snapshot_queries_do_not_need_the_original_checkout(tmp_path):
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "app.py").write_text("def root():\n return child()\n\ndef child():\n return 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    _graph(repo / ".codegraph" / "codegraph.db")
    data = tmp_path / "data"
    store = AnalysisSnapshotSQLiteStore(data / "analysis.db")
    scope = store.create_scope("scope")
    member = RepositoryCatalogService(store).add_member(scope.id, "checkout", str(repo))
    run = store.create_run([member.id])
    attempt = store.claim_next_attempt("test-worker")
    artifacts = FileSystemArtifactStore(data)
    RepositoryAttemptWorker(store, artifacts, command_runner=_Runner(), analyzer=lambda *_: {"answer": 42})(attempt)
    assert store.get_attempt(attempt.id).status is AttemptStatus.COMPLETED
    snapshot = store.get_current_snapshot(member.id)
    assert snapshot

    # This is the Phase-2 acceptance condition: all inputs now come from CAS.
    repo.rename(tmp_path / "checkout-moved")
    service = SnapshotQueryService(SnapshotBundleResolver(store, artifacts))
    assert service.knowledge(snapshot.id) == {"answer": 42}
    tree = service.call_tree_children(snapshot.id, "root")
    assert tree["children"][0]["node_id"] == "child"
    context = service.node_rule_context(snapshot.id, "child")
    assert context and "def child" in context["snippet"]

    runtime = type("Runtime", (), {"snapshot_queries": service, "store": store})()
    mcp_server.set_context(runtime)
    symbols = json.loads(mcp_server.search_snapshot_symbols(snapshot.id, "child"))
    assert [item["name"] for item in symbols["symbols"]] == ["child"]
    assert json.loads(mcp_server.get_snapshot_call_tree(snapshot.id, "root"))["children"][0]["id"] == "child"
    chat = SnapshotChatService(AuditStore(str(data / "audit.db")), service)
    answer = chat.ask(snapshot.id, "谁调用 child？")
    assert answer["operations"][0]["source"] == "snapshot"
    assert answer["operations"][0]["rows"][0]["name"] == "root"
    explanation = SnapshotExplanationSource(service).load(
        {"repository_snapshot_id": snapshot.id, "method": "GET", "path": "/items", "handler": "root"}
    )
    assert explanation["repository_snapshot_id"] == snapshot.id
    assert set(explanation["nodes"]) == {"root", "child"}


def test_knowledge_cli_requires_a_snapshot_selector(monkeypatch, capsys):
    class _Queries:
        def knowledge(self, snapshot_id, *, section=None):
            assert snapshot_id == "frozen-1"
            assert section == "api"
            return {"api_contract": {"endpoints": []}}

    class _Runtime:
        snapshot_queries = _Queries()

        def __init__(self, _root):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "SnapshotRuntime", _Runtime)
    monkeypatch.setattr("sys.argv", ["codeevolution", "knowledge", "--snapshot-id", "frozen-1", "--section", "api"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"api_contract": {"endpoints": []}}
