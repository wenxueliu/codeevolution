"""Bounded in-process scheduler for durable repository attempts."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

from codeevolution.domain.analysis_snapshot import AttemptStage, AttemptStatus, RepositoryAttempt
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


class AnalysisScheduler:
    """Claim durable attempts and execute them with a bounded worker pool.

    Durability lives in the store. Threads only provide execution capacity; a
    restart interrupts old active attempts instead of trying to resume them.
    """

    def __init__(
        self,
        store: AnalysisSnapshotSQLiteStore,
        worker: Callable[[RepositoryAttempt], None],
        *,
        concurrency: int = 2,
        poll_seconds: float = 0.25,
        instance_id: str | None = None,
    ):
        if concurrency < 1:
            raise ValueError("scheduler concurrency must be positive")
        self.store = store
        self.worker = worker
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.instance_id = instance_id or str(uuid.uuid4())
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self, *, recover: bool = True) -> None:
        with self._lock:
            if self._threads:
                return
            if recover:
                self.store.recover_interrupted()
            self._stop.clear()
            self._threads = [
                threading.Thread(
                    target=self._run,
                    name=f"analysis-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.concurrency)
            ]
            for thread in self._threads:
                thread.start()

    def notify(self) -> None:
        self._wake.set()

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            threads, self._threads = self._threads, []
            self._stop.set()
            self._wake.set()
        for thread in threads:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        worker_id = f"{self.instance_id}:{threading.current_thread().name}"
        while not self._stop.is_set():
            attempt = self.store.claim_next_attempt(worker_id)
            if attempt is None:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
                continue
            try:
                self.worker(attempt)
            except BaseException as exc:
                current = self.store.get_attempt(attempt.id)
                if current is not None and current.status == AttemptStatus.RUNNING:
                    self.store.transition_attempt(
                        attempt.id,
                        expected_status=AttemptStatus.RUNNING,
                        status=AttemptStatus.FAILED,
                        stage=AttemptStage.FINISHED,
                        error_code="internal_error",
                        error_message=_safe_error(exc),
                    )


def _safe_error(error: BaseException) -> str:
    message = str(error).replace("\n", " ").replace("\r", " ")
    return (message or error.__class__.__name__)[:500]
