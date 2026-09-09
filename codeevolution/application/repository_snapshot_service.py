"""Snapshot history and graph-view application use cases."""

from __future__ import annotations

from typing import Sequence

from codeevolution.domain.analysis_snapshot import GraphView, GraphViewMember
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


class RepositorySnapshotService:
    def __init__(self, store: AnalysisSnapshotSQLiteStore):
        self.store = store

    def list_snapshots(self, member_id: str):
        if self.store.get_member(member_id) is None:
            raise KeyError(member_id)
        return self.store.list_snapshots(member_id)

    def get_snapshot(self, snapshot_id: str):
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        return snapshot

    def update_metadata(
        self,
        snapshot_id: str,
        *,
        label: str,
        note: str,
        pinned: bool,
        expected_version: int,
    ):
        return self.store.update_snapshot_metadata(
            snapshot_id,
            label=label,
            note=note,
            pinned=pinned,
            expected_version=expected_version,
        )


class GraphViewService:
    def __init__(self, store: AnalysisSnapshotSQLiteStore):
        self.store = store

    def create_current(
        self,
        *,
        scope_ids: Sequence[str] | None = None,
        member_ids: Sequence[str] | None = None,
    ) -> GraphView:
        return self.store.create_current_view(scope_ids=scope_ids, member_ids=member_ids)

    def create_explicit(self, members: Sequence[GraphViewMember]) -> GraphView:
        return self.store.create_explicit_view(members)

    def get(self, view_id: str) -> GraphView:
        view = self.store.get_view(view_id)
        if view is None:
            raise KeyError(view_id)
        return view

    def pin(self, view_id: str, *, label: str = "", note: str = "") -> GraphView:
        return self.store.pin_view(view_id, label=label, note=note)
