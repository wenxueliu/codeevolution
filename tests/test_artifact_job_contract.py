import json

from codeevolution.domain.analysis_snapshot import EvidenceBundle, RepositoryAnalysisSnapshot
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


def _store(tmp_path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    store.create_scope("shop", scope_id="scope")
    store.create_member("scope", "orders", "/repos/orders", "path:orders", member_id="orders")
    run = store.create_run(["orders"])
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
