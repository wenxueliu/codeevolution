import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from codeevolution.infrastructure.artifact_store_fs import (
    ArtifactStoreError,
    FileSystemArtifactStore,
    directory_digest,
)
from codeevolution.infrastructure.codegraph_capture import (
    CodeGraphCapture,
    CodeGraphCaptureError,
    freeze_sources,
)
from codeevolution.infrastructure.codegraph_command import CodeGraphCommandRunner
from codeevolution.infrastructure.snapshot_source import SnapshotSourceInventory
from codeevolution.infrastructure.workspace_input_scanner import (
    ScanPolicy,
    WorkspaceInputScanner,
)


def _write_graph(path: Path, reverse: bool = False) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, start_line INTEGER, signature TEXT
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source TEXT, target TEXT, kind TEXT, metadata TEXT,
                line INTEGER, col INTEGER, provenance TEXT
            );
            CREATE TABLE files (path TEXT, content_hash TEXT, language TEXT, size INTEGER);
            """
        )
        nodes = [
            ("volatile-a", "function", "alpha", "pkg::alpha", "src/a.py", 1, "alpha()"),
            ("volatile-b", "function", "beta", "pkg::beta", "src/b.py", 1, "beta()"),
        ]
        for row in reversed(nodes) if reverse else nodes:
            db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", row)
        db.execute(
            "INSERT INTO edges VALUES (1,?,?,?,?,?,?,?)",
            ("volatile-a", "volatile-b", "calls", '{"confidence":1}', 2, 1, "test"),
        )
        files = [("src/a.py", "aaa", "python", 7), ("src/b.py", "bbb", "python", 6)]
        for row in reversed(files) if reverse else files:
            db.execute("INSERT INTO files VALUES (?,?,?,?)", row)


def test_artifact_store_publishes_atomically_and_reuses_matching_content(tmp_path):
    store = FileSystemArtifactStore(tmp_path / "data")
    staging = store.create_staging("attempt-1")
    (staging / "manifest.json").write_text("{}")
    digest = directory_digest(staging)

    key = store.publish(staging, digest)

    assert key == f"sha256:{digest}"
    assert not staging.exists()
    assert (store.open(key) / "manifest.json").read_text() == "{}"
    assert store.open(key).stat().st_mode & 0o777 == 0o700

    retry = store.create_staging("attempt-2")
    (retry / "manifest.json").write_text("{}")
    assert store.publish(retry, digest) == key
    assert not retry.exists()


def test_artifact_store_rejects_wrong_digest_and_unsafe_keys(tmp_path):
    store = FileSystemArtifactStore(tmp_path / "data")
    staging = store.create_staging("attempt")
    (staging / "payload").write_bytes(b"safe")
    with pytest.raises(ArtifactStoreError, match="digest mismatch"):
        store.publish(staging, "0" * 64)
    with pytest.raises(ArtifactStoreError):
        store.open("../outside")


def test_workspace_scanner_only_hashes_inputs_and_excludes_secrets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "app.py").write_text("print('ok')\n")
    (repo / "README.md").write_text("not an analysis input")
    (repo / "settings.yaml").write_text("mode: test\n")
    (repo / ".env").write_text("TOKEN=secret\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    observed = WorkspaceInputScanner(repo).scan()

    assert [entry.path for entry in observed.entries] == ["app.py", "settings.yaml"]
    assert [(entry.path, entry.categories) for entry in observed.entries] == [
        ("app.py", ("indexed",)),
        ("settings.yaml", ("declared",)),
    ]
    assert (".env", "secret_file") in [
        (excluded.path, excluded.reason) for excluded in observed.exclusions
    ]
    assert all("secret" not in repr(value) for value in observed.entries)


def test_workspace_scanner_rejects_escape_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret")
    (repo / "escape.py").symlink_to(outside)

    observed = WorkspaceInputScanner(repo).scan(ScanPolicy(declared_globs=()))

    assert observed.entries == ()
    assert observed.exclusions[0].reason == "unsafe_symlink"


def test_snapshot_source_only_reads_manifest_members_and_checks_integrity(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    raw = b"line one\nline two\n"
    (sources / "allowed.py").write_bytes(raw)
    (sources / "not-listed.py").write_text("no")
    manifest = {
        "sources": [
            {
                "path": "allowed.py",
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "category": ["indexed"],
                "encoding_status": "utf8",
            }
        ]
    }
    inventory = SnapshotSourceInventory(sources, manifest)

    assert inventory.read_text("allowed.py") == raw.decode()
    assert inventory.snippet("allowed.py", 2, 2) == "line two"
    assert inventory.read_text("not-listed.py") is None
    assert inventory.read_text("../not-listed.py") is None
    assert inventory.list_files(categories=["indexed"])[0].path == "allowed.py"

    (sources / "allowed.py").write_text("tampered")
    assert inventory.read_bytes("allowed.py") is None


def test_codegraph_capture_uses_sqlite_backup_and_stable_logical_digest(tmp_path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _write_graph(first)
    _write_graph(second, reverse=True)
    capture = CodeGraphCapture()

    one = capture.capture(first, tmp_path / "one" / "codegraph.db")
    two = capture.capture(second, tmp_path / "two" / "codegraph.db")

    assert one.integrity_check == "ok"
    assert one.logical_graph_digest == two.logical_graph_digest
    assert one.blob_sha256.startswith("sha256:")
    assert one.schema_fingerprint.startswith("sha256:")


def test_codegraph_command_uses_argv_and_selects_init_or_sync(tmp_path):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    runner = CodeGraphCommandRunner(executable="/bin/echo", timeout_seconds=2)

    initialized = runner.init_or_sync(repo)
    assert initialized.succeeded
    assert initialized.argv == ("/bin/echo", "init")
    assert initialized.stdout.strip() == "init"

    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "codegraph.db").touch()
    synced = runner.init_or_sync(repo)
    assert synced.argv == ("/bin/echo", "sync")


def test_freeze_sources_revalidates_observed_bytes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("one\n")
    observation = WorkspaceInputScanner(repo).scan(ScanPolicy(declared_globs=()))

    frozen = freeze_sources(repo, observation.entries, tmp_path / "frozen")
    assert frozen == observation.entries
    assert (tmp_path / "frozen" / "app.py").read_text() == "one\n"

    source.write_text("two\n")
    with pytest.raises(CodeGraphCaptureError, match="source changed"):
        freeze_sources(repo, observation.entries, tmp_path / "changed")


def test_directory_digest_includes_paths_not_only_file_bytes(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a").write_text(json.dumps({"same": True}))
    (right / "b").write_text(json.dumps({"same": True}))
    assert directory_digest(left) != directory_digest(right)
