"""Durable orchestration for endpoint-scoped API explanation batches."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread

from ..semantic.explanation_templates import (
    default_prompt_guidance,
    default_prompt_templates,
    normalize_prompt_templates,
    prompt_digest,
)

TRANSIENT_MARKERS = (
    "timeout", "timed out", "超时", "rate limit", "限流", "429", "temporarily",
    "temporary", "connection", "network", "503", "502",
)

logger = logging.getLogger(__name__)


def endpoint_key(endpoint: dict) -> str:
    return "|".join((str(endpoint.get("method") or "").upper(), str(endpoint.get("path") or ""), str(endpoint.get("handler") or "")))


class ApiExplanationBatchService:
    def __init__(self, store, snapshot_queries, generation_factory, reference_store=None):
        self.store = store
        self.snapshot_queries = snapshot_queries
        self.generation_factory = generation_factory
        self.reference_store = reference_store

    def create(self, repository_snapshot_id: str, *, prompt_profile_id: str = "", concurrency: int = 2) -> dict:
        profile = self._resolve_profile(repository_snapshot_id, prompt_profile_id)
        digest = (
            prompt_digest(profile["prompt_text"], normalize_prompt_templates(profile.get("prompt_templates")))
            if profile
            else prompt_digest(default_prompt_guidance(), default_prompt_templates())
        )
        active = self.store.get_active_batch(repository_snapshot_id, digest)
        if active:
            return self.store.get_batch(active["id"])  # type: ignore[return-value]
        # Batch generation must use the complete contract.  The ordinary
        # persisted knowledge payload may be a legacy snapshot whose endpoint
        # list was capped at 100 entries.
        report = {"api_contract": self.snapshot_queries.api_contract(repository_snapshot_id)}
        items = []
        for raw in report.get("api_contract", {}).get("endpoints", []):
            if not isinstance(raw, dict):
                items.append({
                    "endpoint_key": f"invalid-{len(items)}",
                    "status": "failed",
                    "error_message": "API contract contains an invalid endpoint",
                })
                continue
            item = self._item_from_endpoint(repository_snapshot_id, raw, digest)
            items.append(item)
        return self.store.create_batch(
            repository_snapshot_id,
            profile["id"] if profile else "",
            digest,
            items,
            concurrency=concurrency,
        )

    def retry_failed(self, batch_id: str, *, concurrency: int | None = None) -> dict:
        source = self.store.get_batch(batch_id)
        if source is None:
            raise KeyError(batch_id)
        failed = [item for item in source["items"] if item["status"] == "failed"]
        if not failed:
            raise ValueError("batch has no failed items")
        active = self.store.get_active_batch(source["repository_snapshot_id"], source["prompt_digest"])
        if active:
            return self.store.get_batch(active["id"])  # type: ignore[return-value]
        items = [{
            "endpoint_key": item["endpoint_key"], "method": item["method"], "path": item["path"],
            "handler": item["handler"], "file": item["file"], "line": item["line"],
        } for item in failed]
        return self.store.create_batch(
            source["repository_snapshot_id"], source["prompt_profile_id"], source["prompt_digest"], items,
            concurrency=concurrency or source["concurrency"], retry_of_job_id=batch_id,
        )

    def run_job(self, batch_id: str, submit_item) -> None:
        try:
            job = self.store.start_batch(batch_id)
            logger.info(
                "api explanation batch started batch_id=%s status=%s total=%s concurrency=%s",
                batch_id, job["status"], job.get("total_count"), job.get("concurrency"),
            )
            while job["status"] in {"queued", "running"}:
                if job["cancel_requested"]:
                    logger.info("api explanation batch cancellation observed batch_id=%s", batch_id)
                    self.store.cancel_batch(batch_id)
                    return
                claimed = self.store.claim_batch_items(batch_id, job["concurrency"])
                if not claimed:
                    latest = self.store.get_batch(batch_id)
                    if latest is None or latest["status"] not in {"queued", "running"}:
                        return
                    time.sleep(0.05)
                    job = latest
                    continue
                logger.info(
                    "api explanation batch items claimed batch_id=%s count=%s handlers=%s",
                    batch_id, len(claimed), ",".join(item.get("handler", "") for item in claimed),
                )
                futures = [submit_item(self._run_item, item, job) for item in claimed]
                for future in as_completed(futures):
                    future.result()
                job = self.store.get_batch(batch_id)
                if job is None:
                    return
                logger.info(
                    "api explanation batch progress batch_id=%s status=%s completed=%s failed=%s skipped=%s running=%s queued=%s",
                    batch_id, job["status"], job.get("completed_count"), job.get("failed_count"),
                    job.get("skipped_count"), job.get("running_count"),
                    sum(item.get("status") == "queued" for item in job.get("items", [])),
                )
            logger.info("api explanation batch finished batch_id=%s status=%s", batch_id, job["status"])
        except Exception as error:
            # Keep the job queryable even when initialization itself fails.
            logger.exception("api explanation batch coordinator failed batch_id=%s", batch_id)
            current = self.store.get_batch(batch_id)
            if current and current["status"] in {"queued", "running"}:
                self.store.fail_batch(batch_id, str(error)[:1000])

    def _run_item(self, item: dict, job: dict) -> None:
        spec = {
            "repository_snapshot_id": job["repository_snapshot_id"], "repo": "",
            "member": "", "method": item["method"], "path": item["path"],
            "handler": item["handler"], "file": item["file"], "line": item["line"],
            "_prompt_profile_id": job["prompt_profile_id"],
        }
        profile = self.store.get_prompt_profile(job["prompt_profile_id"]) if job["prompt_profile_id"] else None
        if profile:
            templates = normalize_prompt_templates(profile.get("prompt_templates"))
            spec.update({"_prompt_text": profile["prompt_text"], "_prompt_version": f"v{profile['version']}",
                         "_prompt_digest": prompt_digest(profile["prompt_text"], templates),
                         "_prompt_templates": templates})
        else:
            templates = default_prompt_templates()
            guidance = default_prompt_guidance()
            spec.update({"_prompt_text": guidance, "_prompt_version": "system-default",
                         "_prompt_digest": prompt_digest(guidance, templates),
                         "_prompt_templates": templates})
        snapshot_id = None
        logger.info(
            "api explanation batch item started batch_id=%s item_id=%s endpoint=%s %s handler=%s attempt=%s",
            job.get("id"), item.get("id"), item.get("method"), item.get("path"),
            item.get("handler"), item.get("attempt_count"),
        )
        try:
            service = self.generation_factory()
            snapshot_id, frozen = service.prepare(spec)
            # Register the candidate before entering the model call.  This
            # lets a concurrent batch cancellation mark the snapshot itself
            # cancelled and lets the generation service stop at a safe point.
            self.store.update_batch_item(
                item["id"], "running", explanation_snapshot_id=snapshot_id
            )
            if self.store.is_batch_cancel_requested(item["batch_id"]):
                self.store.cancel(snapshot_id)
            logger.info(
                "api explanation batch item prepared batch_id=%s item_id=%s snapshot_id=%s nodes=%s edges=%s",
                job.get("id"), item.get("id"), snapshot_id, len(frozen.get("nodes", {})),
                len(frozen.get("edges", [])),
            )
            if not self.store.is_batch_cancel_requested(item["batch_id"]):
                service.generate(snapshot_id, frozen)
            generated = self.store.get_snapshot(snapshot_id)
            logger.info(
                "api explanation batch item generation returned batch_id=%s item_id=%s snapshot_id=%s status=%s error=%s",
                job.get("id"), item.get("id"), snapshot_id,
                generated.status if generated else "missing",
                (generated.error if generated else "snapshot not found") or "",
            )
            if self.store.is_batch_cancel_requested(item["batch_id"]):
                self.store.update_batch_item(
                    item["id"], "cancelled", error_message="批量任务已取消"
                )
                return
            if generated and generated.status in {"completed", "partial"}:
                self.store.update_batch_item(item["id"], "completed", explanation_snapshot_id=snapshot_id)
                if self.reference_store and job["repository_snapshot_id"] and hasattr(self.reference_store, "make_snapshot_reference_permanent"):
                    self.reference_store.make_snapshot_reference_permanent(
                        job["repository_snapshot_id"], "api_explanation", snapshot_id
                    )
                return
            error = (generated.error if generated else "explanation snapshot was not created") or "generation failed"
            self._failed_or_retry(item, error)
        except Exception as error:
            logger.exception(
                "api explanation batch item failed batch_id=%s item_id=%s snapshot_id=%s handler=%s",
                job.get("id"), item.get("id"), snapshot_id, item.get("handler"),
            )
            if self.store.is_batch_cancel_requested(item["batch_id"]):
                self.store.update_batch_item(
                    item["id"], "cancelled", error_message="批量任务已取消"
                )
            else:
                self._failed_or_retry(item, str(error)[:1000])

    def _failed_or_retry(self, item: dict, error: str) -> None:
        transient = any(marker in error.lower() for marker in TRANSIENT_MARKERS)
        attempts = int(item.get("attempt_count") or 0)
        if transient and attempts < 3:
            logger.warning(
                "api explanation batch item transient failure item_id=%s attempt=%s retrying error=%s",
                item.get("id"), attempts, error,
            )
            self.store.update_batch_item(item["id"], "queued", error_message=error)
            time.sleep(2 if attempts == 1 else 5)
        else:
            logger.error(
                "api explanation batch item terminal failure item_id=%s attempt=%s error=%s",
                item.get("id"), attempts, error,
            )
            self.store.update_batch_item(item["id"], "failed", error_message=error)

    def _resolve_profile(self, snapshot_id: str, profile_id: str) -> dict | None:
        if profile_id:
            profile = self.store.get_prompt_profile(profile_id)
            if profile is None or profile["repository_snapshot_id"] != snapshot_id:
                raise ValueError("prompt profile does not belong to repository snapshot")
            return profile
        return self.store.get_current_prompt_profile(snapshot_id)

    def _item_from_endpoint(self, snapshot_id: str, endpoint: dict, prompt_digest: str) -> dict:
        key = endpoint_key(endpoint)
        method, path, handler = endpoint.get("method"), endpoint.get("path"), endpoint.get("handler")
        if not method or not path or not handler:
            return {"endpoint_key": key or f"invalid-{id(endpoint)}", "method": method, "path": path,
                    "handler": handler, "file": endpoint.get("file"), "line": endpoint.get("line"),
                    "status": "failed", "error_message": "端点信息不完整"}
        existing = self.store.list_snapshots_for_repository_snapshot(snapshot_id, key)
        reusable = next((item for item in existing if item.status == "completed" and item.prompt_digest == prompt_digest), None)
        if reusable:
            return {"endpoint_key": key, "method": method, "path": path, "handler": handler,
                    "file": endpoint.get("file"), "line": endpoint.get("line"), "status": "skipped",
                    "skip_reason": "已存在相同提示词的完成结果", "explanation_snapshot_id": reusable.id}
        return {"endpoint_key": key, "method": method, "path": path, "handler": handler,
                "file": endpoint.get("file"), "line": endpoint.get("line")}


class ApiExplanationBatchScheduler:
    """One process-wide LLM executor with per-job concurrency limits."""
    def __init__(self, service, *, workers: int = 5):
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(workers, 5)), thread_name_prefix="api-explanation")
        self.lock = Lock()
        self.coordinators: dict[str, Thread] = {}
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for batch_id in self.service.store.recover_interrupted_batches():
            self.submit(batch_id)

    def submit(self, batch_id: str):
        with self.lock:
            current = self.coordinators.get(batch_id)
            if current and current.is_alive():
                return current
            coordinator = Thread(target=self.service.run_job, args=(batch_id, self._submit_item),
                                 name=f"api-batch-{batch_id[:12]}", daemon=True)
            self.coordinators[batch_id] = coordinator
            coordinator.start()
            return coordinator

    def _submit_item(self, fn, *args):
        return self.executor.submit(fn, *args)

    def close(self) -> None:
        for coordinator in list(self.coordinators.values()):
            coordinator.join(timeout=30)
        self.executor.shutdown(wait=True, cancel_futures=False)
