"""Composition root for the local repository snapshot runtime."""

from __future__ import annotations

from pathlib import Path

from codeevolution.application.analysis_run_service import (
    AnalysisRunService,
    RepositoryCatalogService,
)
from codeevolution.application.analysis_scheduler import AnalysisScheduler
from codeevolution.application.repository_attempt_worker import RepositoryAttemptWorker
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.artifact_store_fs import FileSystemArtifactStore
from codeevolution.infrastructure.registry_snapshot_migration import (
    legacy_registry_migrated,
    migrate_legacy_registry,
)


class SnapshotRuntime:
    """Own snapshot storage, migration, services, and background workers."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        legacy_registry: str | Path | None = None,
        concurrency: int = 2,
        worker=None,
    ):
        self.data_root = Path(data_root).expanduser().resolve()
        self.store = AnalysisSnapshotSQLiteStore(self.data_root / "analysis-snapshots.db")
        if legacy_registry is not None and not legacy_registry_migrated(self.store):
            migrate_legacy_registry(legacy_registry, self.data_root, self.store)
        self.artifacts = FileSystemArtifactStore(self.data_root)
        self.catalog = RepositoryCatalogService(self.store)
        self.runs = AnalysisRunService(self.store)
        self.worker = worker or RepositoryAttemptWorker(self.store, self.artifacts)
        self.scheduler = AnalysisScheduler(
            self.store, self.worker, concurrency=concurrency
        )

    def start(self) -> None:
        self.scheduler.start(recover=True)

    def notify(self) -> None:
        self.scheduler.notify()

    def close(self) -> None:
        self.scheduler.close()
