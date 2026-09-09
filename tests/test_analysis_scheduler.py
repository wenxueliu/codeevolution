import threading
import time

from codeevolution.application.analysis_scheduler import AnalysisScheduler
from codeevolution.domain.analysis_snapshot import AttemptStage, AttemptStatus, RunStatus
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


def _store(tmp_path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    scope = store.create_scope("shop")
    store.create_member(scope.id, "orders", "/repos/orders", "orders", member_id="orders")
    return store


def test_scheduler_processes_pending_attempt_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    completed = threading.Event()

    def worker(attempt):
        store.transition_attempt(
            attempt.id,
            expected_status=AttemptStatus.RUNNING,
            status=AttemptStatus.COMPLETED,
            stage=AttemptStage.FINISHED,
        )
        completed.set()

    scheduler = AnalysisScheduler(store, worker, concurrency=1, poll_seconds=0.01)
    scheduler.start()
    scheduler.start()
    run = store.create_run(["orders"])
    scheduler.notify()
    assert completed.wait(1)
    scheduler.close()
    scheduler.close()
    assert store.get_run(run.id).status == RunStatus.COMPLETED


def test_scheduler_records_sanitized_worker_failure(tmp_path):
    store = _store(tmp_path)
    def fail(_attempt):
        raise RuntimeError("boom\nsecret detail")

    scheduler = AnalysisScheduler(store, fail, concurrency=1, poll_seconds=0.01)
    scheduler.start()
    run = store.create_run(["orders"])
    scheduler.notify()
    deadline = time.monotonic() + 1
    while store.get_run(run.id).status not in {RunStatus.FAILED, RunStatus.PARTIAL}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    scheduler.close()
    attempt = store.list_attempts(run.id)[0]
    assert attempt.error_code == "internal_error"
    assert attempt.error_message == "boom secret detail"
