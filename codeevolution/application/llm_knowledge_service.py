"""Durable, asynchronous Phase 3 knowledge extraction for snapshots."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

LLM_KNOWLEDGE_STAGES = (
    ("business_descriptions", "extract_business_descriptions", {"limit": 15}),
    ("business_rules", "extract_business_rules_llm", {"limit": 10}),
    ("error_catalog", "extract_error_catalog", {"limit": 15}),
    ("state_machines", "extract_state_machines", {}),
)


class LLMKnowledgeService:
    """Run semantic extraction against immutable snapshot evidence only."""

    def __init__(self, store, snapshot_queries):
        self.store = store
        self.snapshot_queries = snapshot_queries

    def run(self, job_id: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        snapshot_id = job["repository_snapshot_id"]
        self.store.update_llm_knowledge_job(
            job_id,
            status="running",
            progress={"percent": 0, "completed": 0, "total": len(LLM_KNOWLEDGE_STAGES), "stage": "starting"},
        )
        try:
            from codeevolution.knowledge import KnowledgeExtractor

            result = {}
            with self.snapshot_queries.open(snapshot_id) as handle:
                extractor = KnowledgeExtractor(handle.graph, handle.sources)
                total = len(LLM_KNOWLEDGE_STAGES)
                for completed, (stage, method_name, options) in enumerate(LLM_KNOWLEDGE_STAGES, 1):
                    method = getattr(extractor, method_name)
                    result[stage] = method(**options)
                    self.store.update_llm_knowledge_job(
                        job_id,
                        progress={
                            "percent": round(completed / total * 100),
                            "completed": completed,
                            "total": total,
                            "stage": stage,
                        },
                    )
            self.store.update_llm_knowledge_job(
                job_id,
                status="completed",
                progress={"percent": 100, "completed": len(LLM_KNOWLEDGE_STAGES), "total": len(LLM_KNOWLEDGE_STAGES), "stage": "completed"},
                result=result,
                error_message="",
            )
        except Exception as error:
            current = self.store.get_llm_knowledge_job(snapshot_id, job_id=job_id)
            progress = (current or {}).get("progress", {})
            self.store.update_llm_knowledge_job(
                job_id,
                status="failed",
                progress={
                    "percent": progress.get("percent", 0),
                    "completed": progress.get("completed", 0),
                    "total": len(LLM_KNOWLEDGE_STAGES),
                    "stage": "failed",
                },
                error_message=str(error)[:1000],
            )

    def _job(self, job_id: str):
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM llm_knowledge_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return dict(row)


class LLMKnowledgeScheduler:
    """Bounded in-process execution with SQLite-backed recovery and progress."""

    def __init__(self, service, *, workers: int = 1):
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="llm-knowledge")
        self.lock = Lock()
        self.futures = {}

    def start(self) -> None:
        self.service.store.recover_interrupted_llm_knowledge_jobs()
        for job in self.service.store.list_pending_llm_knowledge_jobs():
            self.submit(job["id"])

    def submit(self, job_id: str):
        with self.lock:
            current = self.futures.get(job_id)
            if current is not None and not current.done():
                return current
            future = self.executor.submit(self.service.run, job_id)
            self.futures[job_id] = future
            return future

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
