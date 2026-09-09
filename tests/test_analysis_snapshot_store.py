import sqlite3

import pytest

from codeevolution.domain.analysis_snapshot import (
    AttemptStage,
    AttemptStatus,
    EvidenceBundle,
    GraphViewMember,
    RepositoryAnalysisSnapshot,
    RunStatus,
    ViewAvailability,
    ViewLifecycle,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    SCHEMA_VERSION,
    AnalysisSnapshotSQLiteStore,
    AttemptStateConflictError,
    SnapshotStoreError,
)


@pytest.fixture
def store(tmp_path):
    value = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    scope = value.create_scope("commerce", scope_id="scope")
    value.create_member(scope.id, "orders", "/repos/orders", "path:orders", member_id="orders")
    value.create_member(scope.id, "users", "/repos/users", "path:users", member_id="users")
    return value


def test_schema_migration_is_idempotent_and_configured(tmp_path):
    path = tmp_path / "analysis.db"
    AnalysisSnapshotSQLiteStore(path)
    AnalysisSnapshotSQLiteStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert path.stat().st_mode & 0o777 == 0o600
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(repository_attempts)")}
    assert "uq_active_attempt_per_member" in indexes


def test_catalog_uses_stable_ids_and_active_path_constraint(store):
    assert [item.id for item in store.list_scopes()] == ["scope"]
    assert [item.id for item in store.list_members("scope")] == ["orders", "users"]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_member("scope", "duplicate", "/other", "path:orders")
    retired = store.retire_member("orders")
    assert retired.retired_at is not None
    assert store.get_member("orders").id == "orders"
    assert [item.id for item in store.list_members("scope")] == ["users"]


def test_create_run_records_conflict_without_second_active_attempt(store):
    first = store.create_run(["orders", "users"])
    assert first.status == RunStatus.PENDING
    assert all(item.attempt_id for item in first.members)

    second = store.create_run(["orders", "users"])
    assert second.status == RunStatus.FAILED
    assert all(item.blocking_attempt_id for item in second.members)
    assert store.list_attempts(second.id) == []
    with store.connection() as connection:
        active = connection.execute(
            "SELECT member_id,count(*) FROM repository_attempts WHERE status IN ('pending','running') GROUP BY member_id"
        ).fetchall()
    assert [(row[0], row[1]) for row in active] == [("orders", 1), ("users", 1)]


def test_database_rejects_a_second_active_attempt_even_without_store_lock(store):
    first = store.create_run(["orders"])
    attempt = store.list_attempts(first.id)[0]
    with store.connection() as connection, pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute(
            """INSERT INTO repository_attempts
               (id,run_id,member_id,attempt_no,status,stage,requested_at)
               VALUES('illegal',?,'orders',2,'pending','queued','now')""",
            (first.id,),
        )
    assert store.get_attempt(attempt.id) == attempt


def test_claim_transition_and_complete_recompute_run(store):
    run = store.create_run(["orders"])
    attempt = store.claim_next_attempt("worker-1")
    assert attempt.status == AttemptStatus.RUNNING
    assert attempt.stage == AttemptStage.VALIDATING
    assert store.get_run(run.id).status == RunStatus.RUNNING

    done = store.transition_attempt(
        attempt.id,
        expected_status=AttemptStatus.RUNNING,
        expected_stage=AttemptStage.VALIDATING,
        status=AttemptStatus.COMPLETED,
        stage=AttemptStage.FINISHED,
        progress={"percent": 100},
    )
    assert done.status == AttemptStatus.COMPLETED
    assert done.completed_at is not None
    assert store.get_run(run.id).status == RunStatus.COMPLETED
    with pytest.raises(AttemptStateConflictError):
        store.transition_attempt(
            attempt.id,
            expected_status=AttemptStatus.RUNNING,
            status=AttemptStatus.FAILED,
            stage=AttemptStage.FINISHED,
        )


def test_pending_cancel_is_terminal_and_running_cancel_is_a_request(store):
    pending_run = store.create_run(["orders"])
    pending = store.list_attempts(pending_run.id)[0]
    assert store.request_cancel(pending.id).status == AttemptStatus.CANCELLED
    assert store.get_run(pending_run.id).status == RunStatus.CANCELLED

    running_run = store.create_run(["users"])
    running = store.claim_next_attempt("worker-1")
    assert running.run_id == running_run.id
    requested = store.request_cancel(running.id)
    assert requested.status == AttemptStatus.RUNNING
    assert requested.cancellation_requested_at is not None


def test_restart_recovery_interrupts_active_attempts_and_is_idempotent(store):
    one = store.create_run(["orders"])
    two = store.create_run(["users"])
    store.claim_next_attempt("old-worker")
    assert store.recover_interrupted() == 2
    assert store.get_run(one.id).status == RunStatus.INTERRUPTED
    assert store.get_run(two.id).status == RunStatus.INTERRUPTED
    assert all(
        item.status == AttemptStatus.INTERRUPTED
        for run_id in (one.id, two.id)
        for item in store.list_attempts(run_id)
    )
    assert store.recover_interrupted() == 0


def test_selection_and_metadata_validation_are_atomic(store):
    with pytest.raises(SnapshotStoreError):
        store.create_run(["orders", "missing"])
    with pytest.raises(SnapshotStoreError):
        store.create_run(["orders", "orders"])
    assert store.get_run("missing") is None
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM analysis_runs").fetchone()[0] == 0


def test_idempotency_key_returns_original_run(store):
    first = store.create_run(["orders"], idempotency_key="request-1", tags=("manual",))
    second = store.create_run(["users"], idempotency_key="request-1")
    assert second.id == first.id
    assert second.tags == ("manual",)
    assert [item.member_id for item in second.members] == ["orders"]


def publish(store, member_id: str, version: str = "one"):
    run = store.create_run([member_id])
    attempt = store.claim_next_attempt("worker")
    assert attempt.run_id == run.id
    evidence = EvidenceBundle(
        digest=f"evidence-{member_id}-{version}",
        artifact_key=f"sha256:{member_id}-{version}",
        manifest_schema_version="1",
        source_digest=f"source-{version}",
        graph_digest=f"graph-{version}",
        graph_blob_sha256=f"blob-{version}",
        byte_size=42,
        capture_policy_digest="policy-1",
        capture_completeness="complete",
    )
    snapshot = RepositoryAnalysisSnapshot(
        id=f"snapshot-{member_id}-{version}",
        member_id=member_id,
        evidence_digest=evidence.digest,
        captured_at=f"2026-01-0{1 if version == 'one' else 2}T00:00:00+00:00",
        dirty=False,
        codegraph_version="0.9.1",
        codegraph_schema_digest="schema-1",
        analyzer_bundle_digest="analyzer-1",
        rules_digest="rules-1",
        report_schema_version="1",
        options_digest="options-1",
        facts_digest=f"facts-{version}",
        facts_storage="inline",
        facts={"version": version},
        analysis_completeness="complete",
    )
    return store.publish_snapshot(attempt.id, evidence, snapshot), attempt


def test_publish_is_atomic_and_failed_attempt_does_not_replace_current(store):
    first, _ = publish(store, "orders")
    assert store.get_current_snapshot("orders").id == first.id

    failed_run = store.create_run(["orders"])
    failed_attempt = store.claim_next_attempt("worker")
    assert failed_attempt.run_id == failed_run.id
    store.transition_attempt(
        failed_attempt.id,
        expected_status="running",
        status="failed",
        stage="finished",
        error_code="analysis_failed",
    )
    assert store.get_current_snapshot("orders").id == first.id


def test_unchanged_publish_reuses_snapshot_and_retired_member_is_orphaned(store):
    original, _ = publish(store, "orders")
    run = store.create_run(["orders"])
    attempt = store.claim_next_attempt("worker")
    evidence = EvidenceBundle(
        "evidence-orders-one", "sha256:orders-one", "1", "source-one", "graph-one",
        "blob-one", 42, "policy-1", "complete",
    )
    duplicate = RepositoryAnalysisSnapshot(
        "a-new-id-that-is-not-used", "orders", evidence.digest, "2026-02-01T00:00:00+00:00",
        False, "0.9.1", "schema-1", "analyzer-1", "rules-1", "1", "options-1",
        "facts-one", "inline", "complete", facts={"version": "one"},
    )
    reused = store.publish_snapshot(attempt.id, evidence, duplicate)
    assert reused.id == original.id
    assert store.get_attempt(attempt.id).status == AttemptStatus.UNCHANGED
    assert store.get_run(run.id).status == RunStatus.COMPLETED

    retired_run = store.create_run(["users"])
    retired_attempt = store.claim_next_attempt("worker")
    assert retired_attempt.run_id == retired_run.id
    store.retire_member("users")
    evidence = EvidenceBundle(
        "evidence-users-one", "sha256:users-one", "1", "source-one", "graph-one",
        "blob-one", 42, "policy-1", "complete",
    )
    orphan = RepositoryAnalysisSnapshot(
        "snapshot-users-one", "users", evidence.digest, "2026-01-01T00:00:00+00:00",
        False, "0.9.1", "schema-1", "analyzer-1", "rules-1", "1", "options-1",
        "facts-one", "inline", "complete", facts={},
    )
    assert store.publish_snapshot(retired_attempt.id, evidence, orphan).orphaned is True
    assert store.get_current_snapshot("users") is None


def test_history_explicit_view_is_stable_and_validates_snapshot_ownership(store):
    old, _ = publish(store, "orders", "one")
    current, _ = publish(store, "orders", "two")
    history = store.create_explicit_view(
        [GraphViewMember("orders", 0, old.id, ViewAvailability.AVAILABLE)], view_id="history"
    )
    latest = store.create_current_view(member_ids=["orders"], view_id="latest")
    assert history.members[0].snapshot_id == old.id
    assert latest.members[0].snapshot_id == current.id
    assert store.get_view("history").members[0].snapshot_id == old.id
    assert history.expires_at is not None

    with pytest.raises(SnapshotStoreError, match="snapshot_view_member_mismatch"):
        store.create_explicit_view(
            [GraphViewMember("users", 0, old.id, ViewAvailability.AVAILABLE)]
        )


def test_pin_view_changes_reference_lifetime_without_changing_digest(store):
    snapshot, _ = publish(store, "orders")
    view = store.create_current_view(scope_ids=["scope"])
    assert view.completeness == "incomplete"  # users has not been parsed
    assert view.lifecycle == ViewLifecycle.EPHEMERAL
    pinned = store.pin_view(view.id, label="release")
    assert pinned.digest == view.digest
    assert pinned.lifecycle == ViewLifecycle.PINNED
    assert pinned.expires_at is None
    with store.connection() as connection:
        reference = connection.execute(
            """SELECT owner_type,state,expires_at FROM snapshot_references
               WHERE snapshot_id=? AND owner_id=?""",
            (snapshot.id, view.id),
        ).fetchone()
    assert tuple(reference) == ("pinned_view", "permanent", None)
