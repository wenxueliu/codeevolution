from types import SimpleNamespace

import pytest

from codeevolution.analysis.communication.schema import RepositoryCommunicationArtifact
from codeevolution.application.graph_view_resolver import (
    GraphViewResolutionError,
    GraphViewResolver,
)
from codeevolution.domain.analysis_snapshot import GraphViewMember, ViewAvailability
from codeevolution.domain.topology import UnavailableMember


class _Store:
    def __init__(self, view, snapshots):
        self.view = view
        self.snapshots = snapshots

    def get_view(self, view_id):
        return self.view if view_id == self.view.id else None

    def get_snapshot(self, snapshot_id):
        return self.snapshots.get(snapshot_id)


def _view(*members):
    return SimpleNamespace(
        id="view-1", digest="sha256:view", scope_id="scope-1", members=list(members)
    )


def _member(member_id, snapshot_id, availability=ViewAvailability.AVAILABLE):
    return GraphViewMember(member_id, 0, snapshot_id, availability, member_id, (f"{member_id}.internal",))


def _snapshot(snapshot_id, facts):
    return SimpleNamespace(id=snapshot_id, deletion_state="active", facts=facts, facts_digest="sha256:facts")


def test_resolver_returns_unparsed_for_old_snapshot_without_communication_facts():
    member = _member("orders", "snap-orders")
    resolver = GraphViewResolver(_Store(_view(member), {"snap-orders": _snapshot("snap-orders", {})}))

    resolved, artifacts = resolver.resolve("view-1")

    assert artifacts == {}
    assert isinstance(resolved.members[0], UnavailableMember)
    assert resolved.members[0].reason == "communication_facts_unavailable"


def test_resolver_loads_artifact_only_for_frozen_summary_key():
    member = _member("orders", "snap-orders")
    artifact = RepositoryCommunicationArtifact("snap-orders", "sha256:rules")
    snapshot = _snapshot(
        "snap-orders",
        {"communication_summary": {"artifact_key": "sha256:artifact"}},
    )
    resolver = GraphViewResolver(
        _Store(_view(member), {"snap-orders": snapshot}),
        communication_loader=lambda item: artifact,
    )

    resolved, artifacts = resolver.resolve("view-1")

    assert artifacts["snap-orders"] == artifact
    assert resolved.members[0].communication_artifact_key == "sha256:artifact"


def test_resolver_rejects_missing_bound_snapshot_artifact():
    member = _member("orders", "snap-orders")
    snapshot = _snapshot(
        "snap-orders",
        {"communication_summary": {"artifact_key": "sha256:artifact"}},
    )
    resolver = GraphViewResolver(
        _Store(_view(member), {"snap-orders": snapshot}), communication_loader=lambda item: None
    )

    with pytest.raises(GraphViewResolutionError, match="snapshot_artifact_corrupt"):
        resolver.resolve("view-1")


def test_resolver_rejects_legacy_mixed_scope_view():
    view = SimpleNamespace(id="view-1", digest="sha256:view", scope_id=None, members=[])
    with pytest.raises(GraphViewResolutionError, match="legacy_mixed_scope_view"):
        GraphViewResolver(_Store(view, {})).resolve("view-1")
