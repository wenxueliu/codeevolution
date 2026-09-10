from threading import Event

from codeevolution.application.graph_artifact_scheduler import GraphArtifactScheduler


class _Store:
    def __init__(self):
        self.recovered = 0

    def recover_interrupted_artifact_jobs(self):
        self.recovered += 1
        return 0


class _Service:
    def __init__(self):
        self.store = _Store()
        self.calls = []
        self.started = Event()
        self.release = Event()

    def run_job(self, job_id):
        self.calls.append(job_id)
        self.started.set()
        self.release.wait(timeout=2)
        return {"id": job_id, "status": "completed"}


def test_scheduler_recovers_and_deduplicates_active_submission():
    service = _Service()
    scheduler = GraphArtifactScheduler(service)
    try:
        first = scheduler.submit("job-1")
        assert service.started.wait(timeout=2)
        second = scheduler.submit("job-1")
        service.release.set()
        assert first.result(timeout=2)["status"] == "completed"
        assert second is first
        assert service.calls == ["job-1"]
        assert service.store.recovered == 1
    finally:
        scheduler.close()
