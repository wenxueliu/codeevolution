from pathlib import Path

from codeevolution.application.snapshot_retention_service import SnapshotRetentionService
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore


def test_scavenge_does_not_remove_active_attempt_staging(tmp_path: Path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    artifacts = FileSystemArtifactStore(tmp_path)
    scope = store.create_scope("mall")
    member = store.create_member(scope.id, "mall", str(tmp_path), str(tmp_path))
    run = store.create_run([member.id])
    attempt_id = run.members[0].attempt_id
    staging = artifacts.create_staging(attempt_id)
    staging.touch()
    staging_dir = staging.stat().st_mtime

    result = SnapshotRetentionService(store, artifacts).scavenge_orphans(staging_age_seconds=0)

    assert result["staging_removed"] == []
    assert staging.exists()
    assert staging.stat().st_mtime == staging_dir
