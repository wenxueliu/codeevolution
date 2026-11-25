"""API-level integration coverage for the ADR knowledge dimension.

The dimension reads the analyzed repository's own ``docs/adr`` at analysis
time, so the test drives the real capture/analyze/publish flow and then reads
the published facts through both the query service and the HTTP API.
"""

import sqlite3
import subprocess
from fastapi.testclient import TestClient

from codeevolution import api as api_module
from codeevolution.api import create_app
from codeevolution.application.repository_attempt_worker import RepositoryAttemptWorker
from codeevolution.application.snapshot_query_service import SnapshotQueryService
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore
from codeevolution.infrastructure.codegraph_command import CommandResult
from codeevolution.infrastructure.snapshot_bundle_resolver import SnapshotBundleResolver

ADR_FILES = {
    "0001-use-sqlite.md": """\
---
status: accepted
date: 2026-01-02
decision-makers: [alice]
---

# Use SQLite for the snapshot store

## Context and Problem Statement

Need embedded durable storage.

## Decision Outcome

Chosen option: "SQLite", because it ships with Python.
""",
    "0002-adopt-madr.md": """\
---
status: superseded by ADR-0003
date: 2026-01-03
---

# Adopt MADR

## Context and Problem Statement

Decision records need one template.

## Decision Outcome

Chosen option: "MADR 3", because it was current then.
""",
    "0003-madr-4.md": """\
---
status: accepted
date: 2026-01-04
supersedes: [ADR-0002]
---

# Adopt MADR 4.0

## Context and Problem Statement

MADR 4.0 keeps status in frontmatter.

## Decision Outcome

Chosen option: "MADR 4.0", because frontmatter is machine-readable.
""",
    "0004-legacy-status.md": """\
# Record status in a section

## Status

accepted

## Context and Problem Statement

Older adr-tools templates keep status in the body.

## Decision Outcome

Keep the fallback.
""",
    "0005-broken.md": """\
---
status: proposed
---

# Unfinished decision

## Context and Problem Statement

Still drafting.

## Considered Options

* <!-- option -->
""",
}


def _graph(path):
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, start_line INTEGER, signature TEXT
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
                metadata TEXT, line INTEGER, col INTEGER, provenance TEXT
            );
            CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, size INTEGER);
            INSERT INTO nodes VALUES ('n1','function','main','main','app.py',1,'main()');
            INSERT INTO files VALUES ('app.py','hash','python',12);
            """
        )


class _Runner:
    def init_or_sync(self, _root, _cancellation):
        return CommandResult(("codegraph", "sync"), 0, "", "", 0.01)


def _run_analysis(store, artifacts):
    member = store.list_members()[0]
    store.create_run([member.id])
    attempt = store.claim_next_attempt("worker")
    worker = RepositoryAttemptWorker(
        store,
        artifacts,
        command_runner=_Runner(),
        analyzer=lambda *_: {"api_contract": {"endpoint_count": 0, "endpoints": []}},
        analyzer_bundle_digest="analyzer:test",
    )
    worker(attempt)
    snapshot = store.get_current_snapshot("repo")
    assert snapshot is not None
    return snapshot


def _publish_snapshot(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    adr_dir = repo / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    for name, content in ADR_FILES.items():
        (adr_dir / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    _graph(repo / ".codegraph" / "codegraph.db")

    data_root = tmp_path / "data"
    store = AnalysisSnapshotSQLiteStore(data_root / "analysis.db")
    artifacts = FileSystemArtifactStore(data_root)
    scope = store.create_scope("shop")
    store.create_member(scope.id, "repo", str(repo), str(repo), member_id="repo")
    snapshot = _run_analysis(store, artifacts)
    queries = SnapshotQueryService(SnapshotBundleResolver(store, artifacts))
    return repo, store, artifacts, snapshot, queries


def test_adr_facts_are_published_with_records_and_structural_checks(tmp_path):
    _repo, _store, _artifacts, snapshot, queries = _publish_snapshot(tmp_path)

    adr = snapshot.facts["adr"]
    assert adr["available"] is True
    assert adr["adr_dir"] == "docs/adr"
    assert adr["total"] == 5
    assert adr["by_status"]["accepted"] == 3
    assert adr["by_status"]["proposed"] == 1
    assert adr["index_present"] is False

    records = {record["id"]: record for record in adr["records"]}
    assert records["ADR-0002"]["superseded_by"] == 3
    assert records["ADR-0003"]["supersedes"] == [2]
    assert records["ADR-0004"]["status"] == "accepted"  # legacy ## Status fallback
    assert records["ADR-0005"]["path"] == "docs/adr/0005-broken.md"

    rules = {(issue["rule"], issue["level"]) for issue in adr["violations"]}
    assert ("R1.8", "error") in rules  # unfilled template placeholder
    assert ("R1.9", "warning") in rules  # missing Decision Outcome
    assert adr["error_count"] >= 1

    section = queries.knowledge(snapshot.id, section="adr")
    assert section["adr"]["total"] == 5
    assert queries.knowledge(snapshot.id)["adr"]["adr_dir"] == "docs/adr"


def test_knowledge_api_serves_the_adr_section(tmp_path, monkeypatch):
    _repo, _store, _artifacts, snapshot, queries = _publish_snapshot(tmp_path)
    monkeypatch.setattr(api_module, "get_snapshot_query_service", lambda: queries)

    with TestClient(create_app({})) as client:
        response = client.get(
            "/api/knowledge",
            params={"snapshot_id": snapshot.id, "section": "adr"},
        )
        assert response.status_code == 200
        payload = response.json()["adr"]
        assert payload["available"] is True
        assert payload["total"] == 5
        assert payload["records"][0]["id"] == "ADR-0001"


def test_adr_edit_produces_a_new_snapshot_instead_of_reusing_stale_facts(tmp_path):
    repo, store, artifacts, first, _queries = _publish_snapshot(tmp_path)
    assert first.facts["adr"]["by_status"]["proposed"] == 1

    (repo / "docs/adr/0005-broken.md").write_text(
        """\
---
status: accepted
date: 2026-01-05
---

# Finish the decision

## Context and Problem Statement

Drafting is done.

## Decision Outcome

Chosen option: "ship it", because it is ready.
""",
        encoding="utf-8",
    )

    second = _run_analysis(store, artifacts)

    assert second.id != first.id
    assert second.rules_digest != first.rules_digest
    records = {record["id"]: record for record in second.facts["adr"]["records"]}
    assert records["ADR-0005"]["status"] == "accepted"
    assert second.facts["adr"]["error_count"] == 0


def test_snapshots_without_adr_facts_report_unavailable():
    service = SnapshotQueryService(resolver=None)
    facts = service._with_adr({"api_contract": {"endpoints": []}})

    assert facts["adr"]["available"] is False
    assert facts["adr"]["records"] == []
    assert facts["api_contract"] == {"endpoints": []}
    # A report that already carries ADR facts is left untouched.
    existing = {"adr": {"version": "adr/v1", "available": True}}
    assert service._with_adr(existing) is existing
