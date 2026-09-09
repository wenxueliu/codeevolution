"""Open immutable repository-analysis evidence without touching its workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from codeevolution.domain.analysis_snapshot import RepositoryAnalysisSnapshot
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore
from codeevolution.infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
from codeevolution.infrastructure.snapshot_source import SnapshotSourceInventory


class SnapshotUnavailableError(RuntimeError):
    """A snapshot is absent, deleted, or its immutable evidence is damaged."""


@dataclass
class RepositorySnapshotHandle:
    """The only object analysis/query code receives for a repository snapshot."""

    snapshot: RepositoryAnalysisSnapshot
    artifact_root: Path
    manifest: dict
    graph: SQLiteCodeGraphRepository
    sources: SnapshotSourceInventory

    def close(self) -> None:
        self.graph.close()

    def __enter__(self) -> "RepositorySnapshotHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class SnapshotBundleResolver:
    def __init__(self, store: AnalysisSnapshotSQLiteStore, artifacts: FileSystemArtifactStore):
        self.store = store
        self.artifacts = artifacts

    def open(self, snapshot_id: str, *, member_id: str | None = None) -> RepositorySnapshotHandle:
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        if member_id is not None and snapshot.member_id != member_id:
            raise SnapshotUnavailableError("snapshot does not belong to repository member")
        if snapshot.deletion_state != "active":
            raise SnapshotUnavailableError("snapshot has been deleted")
        evidence = self.store.get_evidence(snapshot.evidence_digest)
        if evidence is None or evidence.deletion_state != "active":
            raise SnapshotUnavailableError("snapshot evidence is unavailable")
        try:
            root = self.artifacts.open(evidence.artifact_key)
            raw_manifest = (root / "manifest.json").read_bytes()
            manifest = json.loads(raw_manifest)
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            actual_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
            if actual_digest != evidence.digest:
                raise ValueError("manifest digest does not match evidence")
            graph_path = root / "codegraph.db"
            if not graph_path.is_file():
                raise ValueError("frozen graph is missing")
            expected = str(manifest.get("codegraph", {}).get("blob_sha256", "")).removeprefix("sha256:")
            if expected and hashlib.sha256(graph_path.read_bytes()).hexdigest() != expected:
                raise ValueError("frozen graph checksum mismatch")
            sources = SnapshotSourceInventory(root / "sources", manifest)
            graph = SQLiteCodeGraphRepository(str(graph_path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SnapshotUnavailableError("snapshot evidence is unavailable or corrupt") from error
        return RepositorySnapshotHandle(snapshot, root, manifest, graph, sources)
