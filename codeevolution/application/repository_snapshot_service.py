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
        if snapshot.deletion_state == "trashed":
            raise RuntimeError("snapshot_gone")
        if snapshot.deletion_state == "deletion_pending":
            raise RuntimeError("snapshot_deletion_pending")
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

    def deletion_preview(self, snapshot_id: str) -> dict:
        snapshot = self.get_snapshot(snapshot_id)
        refs = self.store.snapshot_references(snapshot_id)
        return {"snapshot_id": snapshot_id, "protected": bool(refs), "references": refs,
                "estimated_release_bytes": 0 if refs else self.store.get_evidence(snapshot.evidence_digest).byte_size}


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

    def refresh(self, view_id: str) -> GraphView:
        view = self.get(view_id)
        selector = view.selector
        scope_ids = selector.get("scope_ids")
        if selector.get("scope_id") is not None:
            scope_ids = [selector["scope_id"]]
        return self.store.create_current_view(
            scope_ids=scope_ids,
            member_ids=selector.get("member_ids") if selector.get("member_ids") is not None else None,
        )

    def export(self, view_id: str) -> dict:
        view = self.get(view_id)
        if view.lifecycle.value != "pinned":
            raise ValueError("view_not_pinned")
        return {
            "schema": "codeevolution.graph-view.v1", "view_id": view.id,
            "view_digest": view.digest, "scope_id": view.scope_id,
            "topology_rules_digest": view.topology_rules_digest, "created_at": view.created_at,
            "members": [
                {"member_id": item.member_id, "repository_snapshot_id": item.snapshot_id,
                 "availability": item.availability.value, "display_name": item.display_name,
                 "declared_aliases": list(item.declared_aliases)}
                for item in view.members
            ],
        }
