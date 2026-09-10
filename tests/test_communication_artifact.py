import json

import pytest

from codeevolution.analysis.communication import (
    CollectorCoverage,
    CollectorResult,
    CollectorStatus,
    CommunicationArtifactReference,
    CommunicationObservation,
    CommunicationSchemaError,
    EntryFact,
    Location,
    NodeRef,
    RepositoryCommunicationArtifact,
    build_communication_artifact,
    deserialize_communication_artifact,
    serialize_communication_artifact,
)


def _node() -> NodeRef:
    return NodeRef(
        snapshot_id="snapshot-1",
        node_id="node-1",
        kind="function",
        name="create_order",
        qualified_name="orders.api.create_order",
        location=Location("src/orders/api.py", 42, 5, 42, 61),
    )


def _entry() -> EntryFact:
    return EntryFact(
        entry_id="entry:order-create",
        kind="http",
        protocol="http",
        method="POST",
        path_template="/orders",
        handler=_node(),
    )


def _http_observation(identifier: str = "http:order-create") -> CommunicationObservation:
    return CommunicationObservation(
        observation_id=identifier,
        entry_id="entry:order-create",
        caller=_node(),
        callsite=Location("src/orders/client.py", 12, 1),
        extraction_confidence=0.96,
        payload={
            "client": {"library": "httpx", "operation": "post"},
            "request": {
                "method": "POST",
                "authority": "users",
                "normalized_path": "/users/{param}",
                "query_keys": [],
            },
        },
    )


def _coverage(status: CollectorStatus = CollectorStatus.COMPLETE) -> CollectorCoverage:
    return CollectorCoverage("http", "http/v1", status, coverage={"files": 2})


def test_artifact_serializes_deterministically_and_round_trips():
    artifact = RepositoryCommunicationArtifact(
        snapshot_id="snapshot-1",
        rules_digest="sha256:rules",
        entries=(_entry(),),
        http_outbounds=(_http_observation("http:z"), _http_observation("http:a")),
        collector_coverage=(_coverage(),),
    )

    encoded = serialize_communication_artifact(artifact)
    decoded = deserialize_communication_artifact(encoded)
    data = json.loads(encoded)

    assert encoded == serialize_communication_artifact(decoded)
    assert decoded.payload_digest() == artifact.payload_digest()
    assert [item["observation_id"] for item in data["http_outbounds"]] == ["http:a", "http:z"]
    assert data["schema"] == "repository-communication/v1"
    assert data["completeness"] == "complete"


def test_build_merges_collector_results_and_exposes_summary():
    artifact = build_communication_artifact(
        snapshot_id="snapshot-1",
        rules_digest="sha256:rules",
        results=(
            CollectorResult(
                _coverage(), entries=(_entry(),), http_outbounds=(_http_observation(),)
            ),
            CollectorResult(
                CollectorCoverage(
                    "grpc", "grpc/v1", CollectorStatus.UNSUPPORTED, reason="typescript"
                ),
            ),
        ),
    )
    payload = serialize_communication_artifact(artifact)
    summary = artifact.summary(
        CommunicationArtifactReference(
            "sha256:" + "a" * 64, artifact.payload_digest(), len(payload)
        )
    )

    assert artifact.completeness == "partial"
    assert summary["observation_counts"] == {
        "entries": 1,
        "http_outbounds": 1,
        "message_publications": 0,
        "message_subscriptions": 0,
        "grpc_clients": 0,
        "grpc_servers": 0,
        "resource_accesses": 0,
        "unresolved_observations": 0,
    }
    assert summary["payload_digest"] == artifact.payload_digest()


def test_schema_rejects_unsafe_paths_unknown_entries_duplicates_and_noncanonical_json():
    with pytest.raises(CommunicationSchemaError, match="relative POSIX"):
        Location("/secret/app.py", 1)
    with pytest.raises(CommunicationSchemaError, match="dot segments"):
        Location("src/../app.py", 1)

    with pytest.raises(CommunicationSchemaError, match="unknown entry"):
        RepositoryCommunicationArtifact(
            snapshot_id="snapshot-1",
            rules_digest="sha256:rules",
            http_outbounds=(_http_observation(),),
        )

    with pytest.raises(CommunicationSchemaError, match="duplicate observation_id"):
        RepositoryCommunicationArtifact(
            snapshot_id="snapshot-1",
            rules_digest="sha256:rules",
            entries=(_entry(),),
            http_outbounds=(_http_observation(), _http_observation()),
        )

    noncanonical = json.dumps(
        RepositoryCommunicationArtifact(
            snapshot_id="snapshot-1", rules_digest="sha256:rules", collector_coverage=(_coverage(),)
        ).to_dict(),
        indent=2,
    )
    with pytest.raises(CommunicationSchemaError, match="not canonical"):
        deserialize_communication_artifact(noncanonical)
