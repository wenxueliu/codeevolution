import sqlite3
import subprocess
from pathlib import Path

from codeevolution.application.repository_attempt_worker import RepositoryAttemptWorker
from codeevolution.domain.analysis_snapshot import AttemptStatus
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore
from codeevolution.infrastructure.codegraph_command import CommandResult


def _graph(path: Path):
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


class SuccessfulRunner:
    def init_or_sync(self, _root, _cancellation):
        return CommandResult(("codegraph", "sync"), 0, "", "", 0.01)


def _setup(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "app.py").write_text("def main():\n    pass\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    _graph(repo / ".codegraph" / "codegraph.db")
    store = AnalysisSnapshotSQLiteStore(tmp_path / "data" / "analysis.db")
    scope = store.create_scope("shop")
    member = store.create_member(scope.id, "repo", str(repo), str(repo), member_id="repo")
    run = store.create_run([member.id])
    attempt = store.claim_next_attempt("worker")
    return repo, store, run, attempt


def test_worker_captures_analyzes_and_publishes_snapshot(tmp_path):
    _repo, store, _run, attempt = _setup(tmp_path)
    worker = RepositoryAttemptWorker(
        store,
        FileSystemArtifactStore(tmp_path / "data"),
        command_runner=SuccessfulRunner(),
        analyzer=lambda _graph, source: {
            "files": [item.path for item in source.list_files()]
        },
        analyzer_bundle_digest="analyzer:test",
    )

    worker(attempt)

    finished = store.get_attempt(attempt.id)
    assert finished.status == AttemptStatus.COMPLETED
    snapshot = store.get_current_snapshot("repo")
    assert snapshot is not None
    assert snapshot.facts["files"] == ["app.py"]
    assert snapshot.facts["communication_summary"]["artifact_key"].startswith("sha256:")
    assert snapshot.observed_head_commit is None  # repository has staged but no committed HEAD


def test_worker_rejects_source_change_without_publishing(tmp_path):
    repo, store, _run, attempt = _setup(tmp_path)

    class MutatingRunner(SuccessfulRunner):
        def init_or_sync(self, root, cancellation):
            (Path(root) / "app.py").write_text("changed\n")
            return super().init_or_sync(root, cancellation)

    worker = RepositoryAttemptWorker(
        store,
        FileSystemArtifactStore(tmp_path / "data"),
        command_runner=MutatingRunner(),
        analyzer=lambda _graph, _source: {},
    )
    worker(attempt)

    failed = store.get_attempt(attempt.id)
    assert failed.status == AttemptStatus.FAILED
    assert failed.error_code == "source_changed_during_capture"
    assert store.get_current_snapshot("repo") is None
