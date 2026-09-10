import json

from codeevolution.analysis.communication.schema import (
    CollectorCoverage,
    CollectorStatus,
    CommunicationObservation,
    EntryFact,
    NodeRef,
    RepositoryCommunicationArtifact,
)
from codeevolution.analysis.topology.http_matcher import KnownExternalRegistry
from codeevolution.analysis.topology.snapshot_builder import TopologyArtifactBuilder
from codeevolution.domain.topology import ResolvedGraphView, SnapshotHandle, UnavailableMember


def _node(snapshot_id: str, name: str) -> NodeRef:
    return NodeRef(snapshot_id, f"node:{name}", "function", name, f"app.{name}")


def _entry(snapshot_id: str, entry_id: str, method: str, path: str, *, protocol: str = "http") -> EntryFact:
    return EntryFact(entry_id, "http" if protocol == "http" else "grpc_server", protocol, _node(snapshot_id, entry_id), method, path)


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


def _coverage():
    return CollectorCoverage("test", "test/v1", CollectorStatus.COMPLETE)


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


def test_builder_materializes_immutable_observation_payload_for_cas_json():
    caller = SnapshotHandle("caller", "snap-caller", "caller", (), "facts", "artifact", "complete")
    target = SnapshotHandle("target", "snap-target", "target", ("target.internal",), "facts", "artifact", "complete")
    artifacts = {
        "snap-caller": _artifact(
            "snap-caller",
            entries=(_entry("snap-caller", "caller.entry", "GET", "/"),),
            http=(_observation(
                "http:json",
                "caller.entry",
                {"request": {"method": "GET", "authority": "target.internal", "normalized_path": "/users/1"}},
            ),),
        ),
        "snap-target": _artifact(
            "snap-target",
            entries=(_entry("snap-target", "target.entry", "GET", "/users/{id}"),),
        ),
    }
    payload = TopologyArtifactBuilder().build(_view(caller, target), artifacts)
    json.dumps(payload)


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


def test_builder_emits_known_external_boundary_with_registry_evidence_only():
    caller = SnapshotHandle("caller", "snap-caller", "caller", (), "facts", "artifact", "complete")
    artifacts = {
        "snap-caller": _artifact(
            "snap-caller",
            entries=(_entry("snap-caller", "caller.entry", "GET", "/caller"),),
            http=(_observation(
                "http:external",
                "caller.entry",
                {"request": {"method": "GET", "authority": "api.stripe.example", "normalized_path": "/charges"}},
            ),),
        ),
    }
    payload = TopologyArtifactBuilder(
        known_external_registry=KnownExternalRegistry.from_value({
            "rules": [{
                "rule_id": "stripe-api",
                "authority": "api.stripe.example",
                "provider": "stripe",
                "source": "deployment-registry",
            }]
        })
    ).build(_view(caller), artifacts)

    assert payload["endpoint_dependencies"] == []
    assert payload["service_projections"] == []
    boundary = payload["boundary_dependencies"][0]
    assert boundary["kind"] == "known_external"
    assert boundary["external_rule_id"] == "stripe-api"
    assert boundary["external_provider"] == "stripe"
    assert boundary["reason"] == "known_external_registry_match"
    assert payload["identity"]["known_external_registry_digest"].startswith("sha256:")


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


def test_message_dependency_keeps_entry_endpoints_for_static_flow():
    producer = SnapshotHandle("producer", "snap-producer", "producer", (), "facts", "artifact", "complete")
    consumer = SnapshotHandle("consumer", "snap-consumer", "consumer", (), "facts", "artifact", "complete")
    publication = _observation("message:pub", "producer.entry", {"messaging": {"channel": "orders.created", "protocol": "kafka", "delivery_semantics": "broadcast"}})
    subscription = _observation("message:sub", "consumer.entry", {"messaging": {"channel": "orders.created", "protocol": "kafka", "delivery_semantics": "broadcast"}})
    artifacts = {
        "snap-producer": _artifact("snap-producer", entries=(_entry("snap-producer", "producer.entry", "POST", "/publish"),), messages=(publication,)),
        "snap-consumer": _artifact("snap-consumer", entries=(_entry("snap-consumer", "consumer.entry", "POST", "/consume"),), subscriptions=(subscription,)),
    }
    payload = TopologyArtifactBuilder().build(_view(producer, consumer), artifacts)
    edge = next(item for item in payload["endpoint_dependencies"] if item["kind"] == "message")
    assert edge["source"]["entry_id"] == "producer.entry"
    assert edge["target"]["entry_ids"] == ["consumer.entry"]


def test_grpc_dependency_requires_authority_alias_and_server_method():
    caller = SnapshotHandle("caller", "snap-caller", "caller", (), "facts", "artifact", "complete")
    users = SnapshotHandle("users", "snap-users", "users", ("users.internal:9090",), "facts", "artifact", "complete")
    client = _observation("grpc:client", "caller.entry", {"rpc": {"authority": "users.internal:9090", "fully_qualified_method": "/users.v1.UserService/GetUser"}})
    server = _entry("snap-users", "users.get", "GetUser", "/users.v1.UserService/GetUser", protocol="grpc")
    artifacts = {
        "snap-caller": _artifact("snap-caller", entries=(_entry("snap-caller", "caller.entry", "GET", "/"),),),
        "snap-users": _artifact("snap-users", entries=(server, ),),
    }
    artifacts["snap-caller"] = RepositoryCommunicationArtifact(
        snapshot_id="snap-caller", rules_digest="sha256:rules", entries=artifacts["snap-caller"].entries,
        grpc_clients=(client,), collector_coverage=artifacts["snap-caller"].collector_coverage,
    )
    payload = TopologyArtifactBuilder().build(_view(caller, users), artifacts)
    assert any(item["kind"] == "grpc" for item in payload["endpoint_dependencies"])


def test_grpc_unknown_authority_uses_explicit_known_external_registry_boundary():
    caller = SnapshotHandle("caller", "snap-caller", "caller", (), "facts", "artifact", "complete")
    client = _observation(
        "grpc:external",
        "caller.entry",
        {"rpc": {"authority": "api.stripe.example:443", "fully_qualified_method": "/stripe.Payments/Charge"}},
    )
    artifacts = {
        "snap-caller": RepositoryCommunicationArtifact(
            snapshot_id="snap-caller",
            rules_digest="sha256:rules",
            entries=(_entry("snap-caller", "caller.entry", "GET", "/"),),
            grpc_clients=(client,),
            collector_coverage=(_coverage(),),
        ),
    }
    payload = TopologyArtifactBuilder(
        known_external_registry=[{
            "rule_id": "stripe-rpc",
            "authority": "api.stripe.example:443",
            "protocols": ["grpc"],
            "provider": "stripe",
        }]
    ).build(_view(caller), artifacts)

    assert payload["endpoint_dependencies"] == []
    boundary = payload["boundary_dependencies"][0]
    assert boundary["kind"] == "known_external"
    assert boundary["external_rule_id"] == "stripe-rpc"
    assert boundary["external_provider"] == "stripe"


def test_competing_message_consumers_are_alternatives_not_confirmed_edges():
    producer = SnapshotHandle("producer", "snap-producer", "producer", (), "facts", "artifact", "complete")
    first = SnapshotHandle("first", "snap-first", "first", (), "facts", "artifact", "complete")
    second = SnapshotHandle("second", "snap-second", "second", (), "facts", "artifact", "complete")
    pub = _observation("message:pub", "producer.entry", {"messaging": {"channel": "orders", "protocol": "kafka", "delivery_semantics": "competing"}})
    sub1 = _observation("message:sub1", "first.entry", {"messaging": {"channel": "orders", "protocol": "kafka", "delivery_semantics": "competing"}})
    sub2 = _observation("message:sub2", "second.entry", {"messaging": {"channel": "orders", "protocol": "kafka", "delivery_semantics": "competing"}})
    artifacts = {
        "snap-producer": _artifact("snap-producer", entries=(_entry("snap-producer", "producer.entry", "GET", "/"),), messages=(pub,)),
        "snap-first": _artifact("snap-first", entries=(_entry("snap-first", "first.entry", "GET", "/"),), subscriptions=(sub1,)),
        "snap-second": _artifact("snap-second", entries=(_entry("snap-second", "second.entry", "GET", "/"),), subscriptions=(sub2,)),
    }
    payload = TopologyArtifactBuilder().build(_view(producer, first, second), artifacts)
    assert payload["endpoint_dependencies"] == []
    assert len(payload["message_alternatives"]) == 1
    assert payload["message_alternatives"][0]["consumer_member_ids"] == ["first", "second"]


def test_message_publisher_without_consumer_is_retained_as_unresolved_candidate():
    producer = SnapshotHandle("producer", "snap-producer", "producer", (), "facts", "artifact", "complete")
    publication = _observation(
        "message:orphan",
        "producer.entry",
        {"messaging": {"channel": "orders.created", "protocol": "kafka", "delivery_semantics": "broadcast"}},
    )
    payload = TopologyArtifactBuilder().build(
        _view(producer),
        {"snap-producer": _artifact(
            "snap-producer",
            entries=(_entry("snap-producer", "producer.entry", "POST", "/publish"),),
            messages=(publication,),
        )},
    )
    assert payload["endpoint_dependencies"] == []
    assert any(item["reason"] == "no_matching_consumer" for item in payload["candidates"])
