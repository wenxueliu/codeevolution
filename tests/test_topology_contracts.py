"""Phase 0 contract tests for deterministic service-topology identities."""

from __future__ import annotations

import pytest

from codeevolution.application.graph_artifact_service import GraphArtifactService
from codeevolution.domain.analysis_snapshot import EvidenceBundle, RepositoryAnalysisSnapshot
from codeevolution.domain.topology import (
    TopologyArtifactRequestSpec,
    canonical_authority,
    canonical_digest,
    canonical_path_template,
    stable_edge_id,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    AnalysisSnapshotSQLiteStore,
    SnapshotStoreError,
)


def _store(tmp_path: object) -> AnalysisSnapshotSQLiteStore:
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")  # type: ignore[operator]
    store.create_scope("one", scope_id="one")
    store.create_scope("two", scope_id="two")
    store.create_member(
        "one", "Orders", "/repos/orders", "path:orders", member_id="orders",
        declared_aliases=("orders.internal", "orders"),
    )
    store.create_member("two", "Users", "/repos/users", "path:users", member_id="users")
    return store


def _publish(store: AnalysisSnapshotSQLiteStore, member_id: str) -> RepositoryAnalysisSnapshot:
    run = store.create_run([member_id])
    attempt = store.claim_next_attempt("test-worker")
    assert attempt.run_id == run.id
    evidence = EvidenceBundle(
        f"evidence-{member_id}", f"sha256:evidence-{member_id}", "1", "source", "graph",
        "blob", 1, "capture", "complete",
    )
    snapshot = RepositoryAnalysisSnapshot(
        f"snapshot-{member_id}", member_id, evidence.digest, "2026-01-01T00:00:00+00:00",
        False, "codegraph", "schema", "analyzer", "snapshot-rules", "report", "options",
        "facts", "inline", "complete", facts={},
    )
    return store.publish_snapshot(attempt.id, evidence, snapshot)


def test_canonicalization_normalizes_transport_identity_and_edge_ids():
    assert canonical_authority("HTTPS://Orders.Internal.:443/api") == "orders.internal"
    assert canonical_path_template("orders/:order_id/items/{item}") == "/orders/{param}/items/{param}"
    assert canonical_digest({"b": 2, "a": "中文"}) == canonical_digest({"a": "中文", "b": 2})
    assert stable_edge_id("http", {"target": "users", "edge_id": "untrusted", "source": "orders"}) == stable_edge_id(
        "http", {"source": "orders", "target": "users"}
    )
    with pytest.raises(ValueError, match="credentials"):
        canonical_authority("https://token@orders.internal")


def test_topology_request_spec_is_order_independent_and_rejects_filtered_variants():
    first = TopologyArtifactRequestSpec(
        "sha256:view", "scope", (("users", "s2", "available"), ("orders", "s1", "available")),
        "sha256:analyzer", "sha256:rules", {},
    )
    second = TopologyArtifactRequestSpec(
        "sha256:view", "scope", (("orders", "s1", "available"), ("users", "s2", "available")),
        "sha256:analyzer", "sha256:rules", {},
    )
    assert first.to_dict()["members"][0]["member_id"] == "orders"
    assert first.cache_key == second.cache_key
    with pytest.raises(ValueError, match="parameters"):
        TopologyArtifactRequestSpec("v", "scope", (("orders", "s1", "available"),), "a", "r", {"channel": "http"})


def test_graph_views_freeze_single_scope_display_name_and_declared_aliases(tmp_path):
    store = _store(tmp_path)
    snapshot = _publish(store, "orders")
    view = store.create_current_view(member_ids=["orders"])
    assert view.scope_id == "one"
    assert view.members[0].snapshot_id == snapshot.id
    assert view.members[0].display_name == "Orders"
    assert view.members[0].declared_aliases == ("orders", "orders.internal")
    assert store.get_view(view.id).digest == view.digest

    with pytest.raises(SnapshotStoreError, match="mixed_scope_members"):
        store.create_current_view(member_ids=["orders", "users"])
    with pytest.raises(SnapshotStoreError, match="exactly one scope"):
        store.create_current_view(scope_ids=["one", "two"])


def test_topology_cache_identity_uses_frozen_view_not_view_id(tmp_path):
    store = _store(tmp_path)
    _publish(store, "orders")
    first = store.create_current_view(member_ids=["orders"], view_id="first")
    second = store.create_current_view(member_ids=["orders"], view_id="second")
    service = GraphArtifactService(store, queries=object())
    first_spec = service.topology_request_spec(first.id)
    second_spec = service.topology_request_spec(second.id)
    assert first.digest == second.digest
    assert first_spec.cache_key == second_spec.cache_key
    assert "view_id" not in first_spec.to_dict()
