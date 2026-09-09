"""Application use cases for durable repository analysis runs."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from codeevolution.domain.analysis_snapshot import (
    AnalysisRun,
    AnalysisScope,
    AttemptStatus,
    RepositoryMember,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    AnalysisSnapshotSQLiteStore,
    SnapshotStoreError,
)

RETRYABLE_STATUSES = frozenset(
    {AttemptStatus.FAILED, AttemptStatus.CANCELLED, AttemptStatus.INTERRUPTED}
)


class RepositoryCatalogService:
    """Manage stable scope/member identities in the snapshot catalog."""

    def __init__(self, store: AnalysisSnapshotSQLiteStore):
        self.store = store

    def list_scopes(self) -> list[AnalysisScope]:
        return self.store.list_scopes()

    def create_scope(self, name: str) -> AnalysisScope:
        try:
            return self.store.create_scope(name)
        except sqlite3.IntegrityError as error:
            raise SnapshotStoreError("scope name is already registered") from error

    def rename_scope(self, scope_id: str, name: str) -> AnalysisScope:
        return self.store.rename_scope(scope_id, name)

    def list_members(self, scope_id: str) -> list[RepositoryMember]:
        if self.store.get_scope(scope_id) is None:
            raise KeyError(scope_id)
        return self.store.list_members(scope_id)

    def add_member(
        self, scope_id: str, display_name: str, registered_path: str
    ) -> RepositoryMember:
        if self.store.get_scope(scope_id) is None:
            raise KeyError(scope_id)
        path = Path(registered_path).expanduser().resolve()
        if not path.is_dir() or not (path / ".git").exists():
            raise SnapshotStoreError(f"not a Git repository: {path}")
        try:
            return self.store.create_member(
                scope_id,
                display_name,
                str(path),
                self._path_identity(path),
            )
        except sqlite3.IntegrityError as error:
            raise SnapshotStoreError("repository member is already registered") from error

    def update_member(
        self,
        member_id: str,
        *,
        display_name: str | None = None,
        registered_path: str | None = None,
    ) -> RepositoryMember:
        if registered_path is None:
            return self.store.update_member(member_id, display_name=display_name)
        path = Path(registered_path).expanduser().resolve()
        if not path.is_dir() or not (path / ".git").exists():
            raise SnapshotStoreError(f"not a Git repository: {path}")
        return self.store.update_member(
            member_id,
            display_name=display_name,
            registered_path=str(path),
            path_identity=self._path_identity(path),
        )

    def retire_member(self, member_id: str) -> RepositoryMember:
        return self.store.retire_member(member_id)

    @staticmethod
    def _path_identity(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))


class AnalysisRunService:
    """Create, inspect, retry, and cancel durable analysis runs."""

    def __init__(self, store: AnalysisSnapshotSQLiteStore):
        self.store = store

    def create_run(
        self,
        member_ids: Sequence[str],
        *,
        external_context: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        idempotency_key: str | None = None,
    ) -> AnalysisRun:
        return self.store.create_run(
            member_ids,
            external_context=external_context,
            tags=tags,
            idempotency_key=idempotency_key,
        )

    def get_run(self, run_id: str) -> AnalysisRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def attempts(self, run_id: str):
        self.get_run(run_id)
        return self.store.list_attempts(run_id)

    def retry_run(
        self, run_id: str, member_ids: Sequence[str] | None = None
    ) -> AnalysisRun:
        run = self.get_run(run_id)
        attempts = {attempt.member_id: attempt for attempt in self.store.list_attempts(run.id)}
        selected = list(member_ids) if member_ids is not None else [
            item.member_id
            for item in run.members
            if (attempt := attempts.get(item.member_id)) is not None
            and attempt.status in RETRYABLE_STATUSES
        ]
        if not selected or len(set(selected)) != len(selected):
            raise SnapshotStoreError("retry member_ids must be non-empty and unique")
        missing = set(selected) - {item.member_id for item in run.members}
        if missing:
            raise SnapshotStoreError("retry members must belong to the original run")
        invalid = [
            member_id
            for member_id in selected
            if member_id not in attempts or attempts[member_id].status not in RETRYABLE_STATUSES
        ]
        if invalid:
            raise SnapshotStoreError("only failed, cancelled, or interrupted attempts can be retried")
        retry_of = {member_id: attempts[member_id].id for member_id in selected}
        return self.store.create_run(
            selected,
            external_context=run.external_context,
            tags=run.tags,
            retry_of=retry_of,
        )

    def cancel_run(self, run_id: str) -> AnalysisRun:
        self.get_run(run_id)
        for attempt in self.store.list_attempts(run_id):
            self.store.request_cancel(attempt.id)
        return self.get_run(run_id)

    def cancel_member(self, run_id: str, member_id: str) -> AnalysisRun:
        run = self.get_run(run_id)
        member = next((item for item in run.members if item.member_id == member_id), None)
        if member is None:
            raise KeyError(member_id)
        if member.attempt_id is not None:
            self.store.request_cancel(member.attempt_id)
        return self.get_run(run_id)
