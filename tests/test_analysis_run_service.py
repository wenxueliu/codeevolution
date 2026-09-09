import subprocess

import pytest

from codeevolution.application.analysis_run_service import (
    AnalysisRunService,
    RepositoryCatalogService,
)
from codeevolution.domain.analysis_snapshot import AttemptStage, AttemptStatus, RunStatus
from codeevolution.infrastructure.analysis_snapshot_sqlite import (
    AnalysisSnapshotSQLiteStore,
    SnapshotStoreError,
)


@pytest.fixture
def services(tmp_path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    catalog = RepositoryCatalogService(store)
    scope = catalog.create_scope("shop")
    repos = []
    for name in ("orders", "users"):
        path = tmp_path / name
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        repos.append(catalog.add_member(scope.id, name, str(path)))
    return store, catalog, AnalysisRunService(store), scope, repos


def test_catalog_validates_git_and_uses_stable_member_id(services, tmp_path):
    _store, catalog, _runs, scope, members = services
    renamed = catalog.update_member(members[0].id, display_name="orders-api")
    assert renamed.id == members[0].id
    assert renamed.display_name == "orders-api"
    with pytest.raises(SnapshotStoreError, match="not a Git repository"):
        catalog.add_member(scope.id, "bad", str(tmp_path / "missing"))


def test_retry_creates_a_new_run_with_audit_link(services):
    store, _catalog, runs, _scope, members = services
    original = runs.create_run([members[0].id])
    attempt = store.claim_next_attempt("worker")
    store.transition_attempt(
        attempt.id,
        expected_status=AttemptStatus.RUNNING,
        status=AttemptStatus.FAILED,
        stage=AttemptStage.FINISHED,
        error_code="analysis_failed",
    )

    retry = runs.retry_run(original.id)

    assert retry.id != original.id
    retried_attempt = store.list_attempts(retry.id)[0]
    assert retried_attempt.retry_of_attempt_id == attempt.id


def test_cancel_run_is_idempotent_and_does_not_cancel_blocker(services):
    store, _catalog, runs, _scope, members = services
    first = runs.create_run([members[0].id])
    blocked = runs.create_run([members[0].id])

    assert runs.cancel_run(blocked.id).status == RunStatus.FAILED
    assert store.list_attempts(first.id)[0].status == AttemptStatus.PENDING
    assert runs.cancel_run(first.id).status == RunStatus.CANCELLED
    assert runs.cancel_run(first.id).status == RunStatus.CANCELLED


def test_retry_rejects_successful_or_foreign_members(services):
    store, _catalog, runs, _scope, members = services
    original = runs.create_run([members[0].id])
    attempt = store.claim_next_attempt("worker")
    store.transition_attempt(
        attempt.id,
        expected_status=AttemptStatus.RUNNING,
        status=AttemptStatus.COMPLETED,
        stage=AttemptStage.FINISHED,
    )
    with pytest.raises(SnapshotStoreError):
        runs.retry_run(original.id)
    with pytest.raises(SnapshotStoreError):
        runs.retry_run(original.id, [members[1].id])
