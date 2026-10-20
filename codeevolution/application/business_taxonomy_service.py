"""Durable, on-demand LLM business classification for one snapshot.

The deterministic classifier only fires when file names carry a business
prefix.  For every other repository the business axis has to be inferred
semantically, so it runs as an explicit, cached job -- never as part of analysis
and never on the read path.

The model is asked two small questions instead of one large one: propose the
domains, then map chunks of file-name-derived entities onto them.  A small
model can answer both; asking it to enumerate hundreds of paths cannot.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from codeevolution.analysis.knowledge.business_taxonomy import (
    TAXONOMY_PROMPT_VERSION,
    assemble_taxonomy,
    build_assignment_prompt,
    build_domain_prompt,
    derive_entities,
    parse_assignments,
    parse_domains,
)
from codeevolution.analysis.knowledge.module_topology import ModuleTopologyExtractor

_CHUNK_SIZE = 40


class BusinessTaxonomyService:
    """Classify one snapshot's source files into business domains."""

    def __init__(self, store, snapshot_queries):
        self.store = store
        self.snapshot_queries = snapshot_queries

    def run(self, job_id: str) -> None:
        job = self.store.get_business_taxonomy_job_by_id(job_id)
        if job is None:
            return
        snapshot_id = job["repository_snapshot_id"]
        hint = str((job.get("progress") or {}).get("hint") or "")
        self._progress(job_id, "inventory", percent=5)
        try:
            from codeevolution.semantic.client import OpenAILLMClient
            from codeevolution.semantic.config import get_llm_config
            from codeevolution.semantic.json_parser import parse_json

            config = get_llm_config()
            if not config:
                raise RuntimeError("请先在 LLM 设置中配置模型和 API Key")
            model = str(config.get("model") or "")

            with self.snapshot_queries.open(snapshot_id) as handle:
                extractor = ModuleTopologyExtractor(handle.graph)
                records = extractor.source_inventory()
                context = self._context(handle, hint)
            if not records:
                raise RuntimeError("这个快照没有可分类的源文件")
            entities = derive_entities(records)
            repository = self._repository_name(snapshot_id)

            inventory_digest = _digest(records)
            cached = self.store.get_business_taxonomy(snapshot_id)
            if (
                cached
                and cached["inventory_digest"] == inventory_digest
                and cached["prompt_version"] == TAXONOMY_PROMPT_VERSION
                and cached["model"] == model
            ):
                self._completed(job_id, cached["payload"], model=model, cached=True)
                return

            client = OpenAILLMClient(config)
            json_mode = {"response_format": {"type": "json_object"}}
            self._progress(job_id, "domains", percent=20)
            domains, issues = self._propose_domains(
                client, parse_json, records, repository, context, json_mode
            )
            if not domains:
                raise RuntimeError("LLM 未能给出可用的业务域")
            self._progress(job_id, "assignment", percent=45)
            assignment = self._assign(
                client, parse_json, entities, domains, json_mode, job_id
            )
            taxonomy = assemble_taxonomy(records, entities, domains, assignment)
            taxonomy["issues"] = (taxonomy.get("issues") or []) + issues

            self.store.save_business_taxonomy(
                snapshot_id,
                model=model,
                prompt_version=TAXONOMY_PROMPT_VERSION,
                inventory_digest=inventory_digest,
                payload=taxonomy,
            )
            self._completed(job_id, taxonomy, model=model, cached=False, issues=issues)
        except Exception as error:  # noqa: BLE001 - surfaced through job state
            self.store.update_business_taxonomy_job(
                job_id,
                status="failed",
                error_message=str(error),
                progress={"percent": 100, "completed": 0, "total": 1, "stage": "failed"},
            )

    @staticmethod
    def _propose_domains(client, parse_json, records, repository, context, json_mode):
        prompt = build_domain_prompt(records, repository=repository, context=context)
        payload = parse_json(client.complete(prompt, max_tokens=1200, temperature=0.1, extra=json_mode))
        return parse_domains(payload)

    def _assign(self, client, parse_json, entities, domains, json_mode, job_id) -> dict[str, str]:
        domain_ids = [domain["id"] for domain in domains]
        allowed = set(domain_ids) | {"shared"}
        keys = list(entities)
        assignment: dict[str, str] = {}
        # A small model drops items from long lists, so leftovers are re-asked
        # instead of being dumped into `shared` on the first miss.
        for round_number in range(1, 4):
            pending = [key for key in keys if key not in assignment]
            if not pending:
                break
            chunks = [
                pending[index : index + _CHUNK_SIZE]
                for index in range(0, len(pending), _CHUNK_SIZE)
            ]
            for number, chunk in enumerate(chunks, 1):
                self._progress(
                    job_id,
                    f"assignment {round_number}.{number}/{len(chunks)}",
                    percent=min(90, 45 + int(45 * (round_number - 1) / 3)),
                )
                prompt = build_assignment_prompt(chunk, domain_ids)
                payload = parse_json(
                    client.complete(prompt, max_tokens=1600, temperature=0.0, extra=json_mode)
                )
                assignment.update(parse_assignments(payload, chunk, allowed))
        return assignment

    def _completed(
        self,
        job_id: str,
        taxonomy: dict[str, Any],
        *,
        model: str,
        cached: bool,
        issues: list[str] | None = None,
    ) -> None:
        self.store.update_business_taxonomy_job(
            job_id,
            status="completed",
            progress={"percent": 100, "completed": 1, "total": 1, "stage": "completed"},
            result={
                "taxonomy": taxonomy,
                "stats": taxonomy.get("stats", {}),
                "model": model,
                "prompt_version": TAXONOMY_PROMPT_VERSION,
                "cached": cached,
                "issues": issues or taxonomy.get("issues", []),
            },
        )

    def _progress(self, job_id: str, stage: str, *, percent: int) -> None:
        self.store.update_business_taxonomy_job(
            job_id,
            status="running",
            progress={"percent": percent, "completed": 0, "total": 1, "stage": stage},
        )

    def _repository_name(self, snapshot_id: str) -> str:
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            return "repository"
        member = self.store.get_member(snapshot.member_id)
        return member.display_name if member is not None else "repository"

    @staticmethod
    def _context(handle, hint: str) -> str:
        parts: list[str] = []
        if hint.strip():
            parts.append(f"用户给定的业务背景：{hint.strip()}")
        try:
            raw = handle.sources.read_bytes("pyproject.toml")
        except Exception:  # noqa: BLE001 - context is optional
            raw = None
        if raw:
            for pattern in (
                r'^\s*description\s*=\s*"([^"]+)"',
                r'^\s*name\s*=\s*"([^"]+)"',
            ):
                match = re.search(pattern, raw.decode("utf-8", "ignore"), re.MULTILINE)
                if match:
                    parts.append(f"项目信息：{match.group(1)}")
                    break
        return "\n".join(parts)


def _digest(records: list[tuple[str, str]]) -> str:
    encoded = json.dumps(records, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class BusinessTaxonomyScheduler:
    """Bounded in-process execution with SQLite-backed recovery and progress."""

    def __init__(self, service: BusinessTaxonomyService, *, workers: int = 1):
        self.service = service
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="business-taxonomy"
        )
        self.lock = Lock()
        self.futures: dict[str, Any] = {}

    def start(self) -> None:
        self.service.store.recover_interrupted_business_taxonomy_jobs()
        for job in self.service.store.list_pending_business_taxonomy_jobs():
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
