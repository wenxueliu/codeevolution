"""Small durable-aware scheduler for explicit Graph Artifact Jobs."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock


class GraphArtifactScheduler:
    """Submit at-most-once local executions while SQLite provides deduplication."""

    def __init__(self, service, *, workers: int = 1):
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="graph-artifact")
        self.lock = Lock()
        self.futures: dict[str, Future] = {}
        self.service.store.recover_interrupted_artifact_jobs()

    def submit(self, job_id: str) -> Future:
        with self.lock:
            current = self.futures.get(job_id)
            if current is not None and not current.done():
                return current
            future = self.executor.submit(self.service.run_job, job_id)
            self.futures[job_id] = future
            return future

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


__all__ = ["GraphArtifactScheduler"]
