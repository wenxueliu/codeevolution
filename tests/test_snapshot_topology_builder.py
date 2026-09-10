from codeevolution.analysis.communication.schema import (
    CollectorCoverage,
    CollectorStatus,
    CommunicationObservation,
    EntryFact,
    NodeRef,
    RepositoryCommunicationArtifact,
)
from codeevolution.analysis.topology.snapshot_builder import TopologyArtifactBuilder
from codeevolution.domain.topology import ResolvedGraphView, SnapshotHandle, UnavailableMember


def _node(snapshot_id: str, name: str) -> NodeRef:
    return NodeRef(snapshot_id, f"node:{name}", "function", name, f"app.{name}")


def _entry(snapshot_id: str, entry_id: str, method: str, path: str) -> EntryFact:
    return EntryFact(entry_id, "http", "http", _node(snapshot_id, entry_id), method, path)


def _artifact(snapshot_id: str, *, entries=(), http=(), messages=(), subscriptions=(), resources=()):
    return RepositoryCommunicationArtifact(
        snapshot_id=snapshot_id,
        rules_digest="sha256:rules",
        entries=tuple(entries),
        http_outbounds=tuple(http),
        message_publications=tuple(messages),
        message_subscriptions=tuple(subscriptions),
        resource_accesses=tuple(resources),
        collector_coverage=(CollectorCoverage("test", "test/v1", CollectorStatus.COMPLETE),),
    )


def _observation(observation_id, entry_id, payload):
    return CommunicationObservation(observation_id, entry_id, None, None, payload, 1.0)


def _view(*members):
    return ResolvedGraphView("scope-1", "view-1", "sha256:view", tuple(members))


def test_builder_creates_confirmed_http_endpoint_and_service_projection():
    orders = SnapshotHandle("orders", "snap-orders", "orders", ("orders.internal",), "facts", "artifact", "complete")
    users = SnapshotHandle("users", "snap-users", "users", ("users.internal",), "facts", "artifact", "complete")
    artifacts = {
        "snap-orders": _artifact(
            "snap-orders",
            entries=(_entry("snap-orders", "orders.entry", "POST", "/orders"),),
            http=(_observation("http:1", "orders.entry", {"request": {"method": "GET", "authority": "users.internal", "normalized_path": "/users/42"}}),),
        ),
        "snap-users": _artifact("snap-users", entries=(_entry("snap-users", "users.entry", "GET", "/users/{id}"),)),
    }

    payload = TopologyArtifactBuilder().build(_view(orders, users), artifacts)

    assert len(payload["endpoint_dependencies"]) == 1
    edge = payload["endpoint_dependencies"][0]
    assert edge["target"]["member_id"] == "users"
    assert payload["service_projections"][0]["source_member_id"] == "orders"
    assert payload["coverage"]["status"] == "complete"


def test_builder_does_not_confirm_relative_url_and_keeps_resource_separate():
    orders = SnapshotHandle("orders", "snap-orders", "orders", (), "facts", "artifact", "complete")
    users = SnapshotHandle("users", "snap-users", "users", ("users.internal",), "facts", "artifact", "complete")
    artifacts = {
        "snap-orders": _artifact(
            "snap-orders",
            entries=(_entry("snap-orders", "orders.entry", "GET", "/orders"),),
            http=(_observation("http:1", "orders.entry", {"request": {"method": "GET", "normalized_path": "/users/42"}}),),
            resources=(_observation("resource:1", "orders.entry", {"type": "redis", "operation": "GET"}),),
        ),
        "snap-users": _artifact("snap-users", entries=(_entry("snap-users", "users.entry", "GET", "/users/{id}"),)),
    }

    payload = TopologyArtifactBuilder().build(_view(orders, users), artifacts)

    assert payload["endpoint_dependencies"] == []
    assert payload["candidates"][0]["kind"] == "unresolved"
    assert len(payload["resource_dependencies"]) == 1


def test_builder_marks_unavailable_member_and_partial_coverage():
    available = SnapshotHandle("orders", "snap-orders", "orders", (), "facts", "artifact", "complete")
    missing = UnavailableMember("users", "users", "unparsed", "no snapshot")
    payload = TopologyArtifactBuilder().build(
        _view(available, missing),
        {"snap-orders": _artifact("snap-orders")},
    )

    assert payload["coverage"]["status"] == "partial"
    assert payload["services"][1]["availability"] == "unparsed"
    assert "users" in payload["coverage"]["unknown_boundaries"]
