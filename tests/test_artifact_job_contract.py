import json
from datetime import datetime, timedelta, timezone

import pytest

from codeevolution.application.graph_artifact_service import (
    GraphArtifactRequestError,
    GraphArtifactService,
)
from codeevolution.domain.analysis_snapshot import EvidenceBundle, RepositoryAnalysisSnapshot
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    AnalysisSnapshotSQLiteStore,
    SnapshotStoreError,
)


def _store(tmp_path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    store.create_scope("shop", scope_id="scope")
    store.create_member("scope", "orders", "/repos/orders", "path:orders", member_id="orders")
    store.create_run(["orders"])
    attempt = store.claim_next_attempt("worker")
    evidence = EvidenceBundle("evidence", "sha256:evidence", "1", "source", "graph", "blob", 1, "capture", "complete")
    snapshot = RepositoryAnalysisSnapshot(
        "snapshot", "orders", "evidence", "2026-01-01T00:00:00+00:00", False, "cg", "schema", "analyzer", "rules", "report", "options", "facts", "inline", "complete", facts={},
    )
    store.publish_snapshot(attempt.id, evidence, snapshot)
    return store


def test_artifact_job_persists_immutable_request_spec_and_releases_input_reference(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    job = store.create_artifact_job(
        view_id=view.id, artifact_kind="topology", cache_key="sha256:cache",
        request_spec={"artifact_kind": "topology", "normalized_params": {}},
    )

    assert json.loads(job["request_spec_json"])["artifact_kind"] == "topology"
    assert job["request_spec_digest"].startswith("sha256:")
    assert store.snapshot_references("snapshot")[0]["owner_id"] == job["id"]
    store.start_artifact_job(job["id"])
    store.complete_artifact_job(job["id"], {"schema_version": "topology-artifact/v1"})
    assert not [item for item in store.snapshot_references("snapshot") if item["owner_type"] == "artifact_job"]


def test_retry_reuses_exact_request_spec(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    job = store.create_artifact_job(
        view_id=view.id, artifact_kind="topology", cache_key="sha256:cache",
        request_spec={"artifact_kind": "topology", "normalized_params": {}, "x": 1},
    )
    store.fail_artifact_job(job["id"], "broken")
    retry = store.retry_artifact_job(job["id"])
    assert retry["request_spec_json"] == job["request_spec_json"]
    assert retry["request_spec_digest"] == job["request_spec_digest"]


def test_artifact_completion_is_fenced_by_worker_lease(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    job = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    running = store.start_artifact_job(job["id"], worker_id="worker-a")
    assert running["lease_token"]
    try:
        store.complete_artifact_job(job["id"], {"x": 1}, lease_token="stale-token")
    except SnapshotStoreError:
        pass
    else:
        raise AssertionError("stale worker must not publish an artifact")
    assert store.get_artifact_job(job["id"])["status"] == "running"
    store.complete_artifact_job(job["id"], {"x": 1}, lease_token=running["lease_token"])


def test_active_artifact_job_creation_is_deduplicated(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    first = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    second = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    assert second["id"] == first["id"]
    assert second["reused"] is True


def test_cache_retention_uses_lru_ttl_and_respects_reader_lease(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    job = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    store.start_artifact_job(job["id"])
    store.complete_artifact_job(job["id"], {"schema_version": "topology-artifact/v1"})
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with store.connection() as connection, connection:
        connection.execute(
            "UPDATE graph_view_artifact_cache SET last_accessed_at=? WHERE cache_key_digest=?",
            (old, "sha256:cache"),
        )

    reader = store.acquire_artifact_cache_reader("sha256:cache")
    assert store.scavenge_artifact_cache(limit=10) == []
    store.release_artifact_cache_reader("sha256:cache", reader)
    removed = store.scavenge_artifact_cache(limit=10)
    assert [item["cache_key_digest"] for item in removed] == ["sha256:cache"]
    assert store.get_artifact_cache("sha256:cache") is None


def test_cache_retention_does_not_remove_cache_with_active_job(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    completed = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    store.start_artifact_job(completed["id"])
    store.complete_artifact_job(completed["id"], {"schema_version": "topology-artifact/v1"})
    pending = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache2")
    assert pending["status"] == "pending"
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with store.connection() as connection, connection:
        connection.execute(
            """INSERT INTO graph_view_artifact_cache
               (cache_key_digest,view_digest,artifact_kind,created_at,last_accessed_at,
                params_digest,analyzer_bundle_digest,rules_digest,artifact_schema_version,
                semantic_identity_digest,payload_storage,payload_json,artifact_key,payload_digest,byte_size)
               VALUES(?,?,?,?,?,'params-2','analyzer-2','rules-2','schema-2','identity-2','inline',?,NULL,?,?)""",
            ("sha256:cache2", view.digest, "topology", old, old, "{}", "sha256:payload2", 2),
        )
    assert store.scavenge_artifact_cache(limit=10) == []


def test_terminal_job_retention_removes_old_metadata_but_not_active_jobs(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    old_job = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:old")
    store.fail_artifact_job(old_job["id"], "old failure")
    active_job = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:active")
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    with store.connection() as connection, connection:
        connection.execute(
            "UPDATE graph_view_artifact_jobs SET completed_at=? WHERE id=?",
            (old, old_job["id"]),
        )
    removed = store.scavenge_artifact_jobs(limit=10)
    assert [item["id"] for item in removed] == [old_job["id"]]
    assert store.get_artifact_job(old_job["id"]) is None
    assert store.get_artifact_job(active_job["id"])["status"] == "pending"


def test_job_status_is_view_authorized_and_does_not_return_inline_payload(tmp_path):
    store = _store(tmp_path)
    view = store.create_current_view(member_ids=["orders"])
    job = store.create_artifact_job(view_id=view.id, artifact_kind="topology", cache_key="sha256:cache")
    store.start_artifact_job(job["id"])
    store.complete_artifact_job(job["id"], {"schema_version": "topology-artifact/v1", "secret": "not-status"})
    service = GraphArtifactService(store, queries=object())
    public = service.get_job(job["id"])
    assert "payload_json" not in public
    assert service.retry_job(job["id"])["status"] == "completed"

    with store.connection() as connection, connection:
        connection.execute(
            "UPDATE graph_views SET expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), view.id),
        )
    with pytest.raises(GraphArtifactRequestError, match="view_expired"):
        service.get_job(job["id"])
