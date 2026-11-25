"""ADR generation use cases: prompt resolution, model calls, drafts and export.

The model is the only source of decision content.  This service owns the
input assembly, the per-service prompt resolution and the trace that makes a
generation debuggable (source -> prompt -> model output -> ADR).

The public entry points are deliberately narrow:

* ``start`` / ``start_history`` queue durable generations (API + scheduler);
* ``generate`` runs one generation synchronously (CLI);
* ``export_draft`` writes a draft into the repository's ADR directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..analysis.knowledge.adr import FILENAME_RE, load_adrs, render_index
from ..semantic.config import get_llm_config
from ..semantic.json_parser import parse_json
from . import git_source
from .domain import (
    PROMPT_SOURCE_KINDS,
    default_prompt,
    decision_schema_text,
    normalize_decision,
    prompt_digest,
    render_madr,
    render_prompt,
)
from .store import AdrStore

logger = logging.getLogger(__name__)

MAX_FILE_CHARS = 120_000
MAX_TEXT_CHARS = 20_000
MAX_HISTORY_CANDIDATES = 100
EXPORT_DIR_CANDIDATES = ("docs/adr", "doc/adr", "docs/decisions", "adr")


class AdrServiceError(RuntimeError):
    """User-facing ADR generation error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _member_field(member: Any, name: str, default: str = "") -> str:
    if isinstance(member, dict):
        value = member.get(name)
    else:
        value = getattr(member, name, None)
    return str(value) if value not in (None, "") else default


def _member_id(member: Any) -> str:
    return _member_field(member, "id")


def _member_name(member: Any) -> str:
    return _member_field(member, "display_name") or _member_id(member)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class AdrService:
    def __init__(
        self,
        store: AdrStore,
        member_loader: Callable[[str], dict[str, Any] | None],
        completer: Callable[[str], str | None] | None = None,
    ):
        self.store = store
        self.member_loader = member_loader
        self._completer = completer

    # ── model ────────────────────────────────────────────────────────

    def llm_available(self) -> bool:
        return get_llm_config() is not None

    def model_name(self) -> str:
        config = get_llm_config() or {}
        return str(config.get("model") or "")

    def _complete(self, prompt: str) -> str:
        if self._completer is not None:
            response = self._completer(prompt)
        else:
            config = get_llm_config()
            if not config:
                raise AdrServiceError(
                    "请先在 LLM 设置中配置模型和 API Key", status=409
                )
            from ..semantic.client import OpenAILLMClient

            response = OpenAILLMClient(config).complete(prompt, max_tokens=3000, temperature=0.2)
        if not response:
            raise AdrServiceError("模型返回空响应", status=502)
        return response

    # ── prompts ──────────────────────────────────────────────────────

    def prompts(self, member_id: str) -> dict[str, Any]:
        stored = {item["source_kind"]: item for item in self.store.list_current_prompts(member_id)}
        kinds = []
        for kind in PROMPT_SOURCE_KINDS:
            current = stored.get(kind)
            kinds.append(
                {
                    "source_kind": kind,
                    "prompt": current,
                    "default_prompt": default_prompt(kind),
                    "prompt_digest": prompt_digest(
                        current["prompt_text"] if current else default_prompt(kind)
                    ),
                }
            )
        return {"member_id": member_id, "kinds": kinds}

    def save_prompt(self, member_id: str, source_kind: str, prompt_text: str) -> dict[str, Any]:
        if source_kind not in PROMPT_SOURCE_KINDS:
            raise AdrServiceError(f"unsupported source kind: {source_kind}")
        text = (prompt_text or "").strip()
        if not text:
            raise AdrServiceError("prompt text is required")
        return self.store.create_prompt(member_id, source_kind, text, prompt_digest(text))

    def reset_prompt(self, member_id: str, source_kind: str) -> bool:
        return self.store.delete_prompts(member_id, source_kind)

    def _resolve_prompt(self, member_id: str, source_kind: str) -> dict[str, Any]:
        kind = source_kind if source_kind in PROMPT_SOURCE_KINDS else "commit"
        stored = self.store.current_prompt(member_id, kind)
        if stored is not None:
            return {
                "id": stored["id"],
                "version": stored["version"],
                "digest": stored["prompt_digest"],
                "text": stored["prompt_text"],
                "origin": "member",
            }
        text = default_prompt(kind)
        return {"id": "", "version": 0, "digest": prompt_digest(text), "text": text, "origin": "default"}

    # ── members / repository ─────────────────────────────────────────

    def member(self, member_id: str) -> dict[str, Any]:
        member = self.member_loader(member_id)
        if not member:
            raise AdrServiceError("repository member not found", status=404)
        return member

    def _repo_root(self, member: Any) -> Path:
        raw = _member_field(member, "registered_path") or _member_field(member, "path")
        root = Path(str(raw)).expanduser()
        if not raw or not root.is_dir():
            raise AdrServiceError("repository checkout is unavailable", status=409)
        return root

    # ── source assembly ──────────────────────────────────────────────

    def _existing_adr_titles(self, repo_root: Path) -> list[str]:
        for candidate in EXPORT_DIR_CANDIDATES:
            directory = repo_root / candidate
            if directory.is_dir():
                return [adr.title or adr.slug for adr in load_adrs(directory)][:30]
        return []

    def _read_repo_file(self, repo_root: Path, relative: str) -> tuple[str, str]:
        candidate = (repo_root / relative).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError as error:
            raise AdrServiceError("file must stay inside the repository") from error
        if not candidate.is_file():
            raise AdrServiceError(f"file not found in repository: {relative}", status=404)
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise AdrServiceError(f"cannot read file: {relative}") from error
        return candidate.relative_to(repo_root.resolve()).as_posix(), content

    def build_source(
        self,
        member: dict[str, Any],
        source: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Normalise a request into a source payload, or raise a skip error."""
        kind = str(source.get("kind") or "").strip()
        repo_root = self._repo_root(member)
        if kind == "commit":
            ref = str(source.get("ref") or "HEAD").strip()
            try:
                commit = git_source.read_commit(repo_root, ref)
            except git_source.AdrGitError as error:
                raise AdrServiceError(str(error), status=400) from error
            if commit.is_merge and not force:
                raise AdrServiceError(f"skip merge commit {commit.sha[:12]}", status=409)
            if not force and not git_source.significant_files(commit.files):
                raise AdrServiceError(
                    f"skip commit {commit.sha[:12]}: only docs/tests/lockfiles changed", status=409
                )
            payload = {
                "commit": {
                    "sha": commit.sha,
                    "author": commit.author,
                    "committed_at": commit.committed_at,
                    "message": commit.message,
                    "files": commit.files,
                    "diff": commit.diff,
                    "diff_truncated": commit.diff_truncated,
                }
            }
            return {"kind": "commit", "ref": commit.sha, "payload": payload}

        if kind == "file":
            filename = str(source.get("filename") or source.get("path") or "").strip()
            content = source.get("content")
            truncated = False
            if content is None or not str(content).strip():
                if not filename:
                    raise AdrServiceError("file path is required")
                filename, content = self._read_repo_file(repo_root, filename)
            text = str(content)
            if len(text) > MAX_FILE_CHARS:
                text = text[:MAX_FILE_CHARS] + "\n... [file truncated]\n"
                truncated = True
            if not text.strip():
                raise AdrServiceError("file is empty")
            payload = {
                "file": {
                    "filename": filename or "uploaded.txt",
                    "content": text,
                    "truncated": truncated,
                }
            }
            return {"kind": "file", "ref": payload["file"]["filename"], "payload": payload}

        if kind == "text":
            text = str(source.get("text") or "").strip()
            if not text:
                raise AdrServiceError("text is required")
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
            payload = {"text": {"text": text}}
            return {"kind": "text", "ref": _digest(text)[:19], "payload": payload}

        raise AdrServiceError(f"unsupported source kind: {kind or '(empty)'}")

    # ── prompt rendering ─────────────────────────────────────────────

    @staticmethod
    def _clean_evidence(decision: dict[str, Any], repo_root: Path) -> None:
        """Keep evidence paths repository-relative so exports stay portable."""
        root = str(repo_root).rstrip("/")
        for item in decision.get("evidence") or []:
            path = str(item.get("path") or "")
            if root and path.startswith(root):
                item["path"] = path[len(root):].lstrip("/")
            elif path.startswith("/"):
                item["path"] = path.rsplit("/", 1)[-1]

    def _prompt_values(
        self,
        member: dict[str, Any],
        repo_root: Path,
        source_kind: str,
        source_ref: str,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        service = _member_name(member)
        values: dict[str, str] = {
            "service": service,
            "repo": str(repo_root),
            "source_kind": source_kind,
            "source_ref": source_ref,
            "schema": decision_schema_text(),
            "existing_adrs": "\n".join(f"* {title}" for title in self._existing_adr_titles(repo_root)),
        }
        if source_kind == "commit":
            commit = payload.get("commit") or {}
            values.update(
                {
                    "commit_message": str(commit.get("message") or ""),
                    "commit_files": "\n".join(str(item) for item in commit.get("files") or []),
                    "diff": str(commit.get("diff") or ""),
                    "author": str(commit.get("author") or ""),
                    "committed_at": str(commit.get("committed_at") or ""),
                }
            )
        elif source_kind == "file":
            file_info = payload.get("file") or {}
            values["content"] = str(file_info.get("content") or "")
        else:
            values["text"] = str((payload.get("text") or {}).get("text") or "")
        return values

    # ── generations ──────────────────────────────────────────────────

    def create_generation(
        self,
        member: dict[str, Any],
        source: dict[str, Any],
        *,
        force: bool = False,
        batch_id: str = "",
    ) -> dict[str, Any]:
        try:
            prepared = self.build_source(member, source, force=force)
        except AdrServiceError as error:
            if error.status != 409:
                raise
            return self._skipped_generation(member, source, str(error), batch_id)
        member_key = _member_id(member)
        prompt = self._resolve_prompt(member_key, prepared["kind"])
        return self.store.create_generation(
            member_id=member_key,
            source_kind=prepared["kind"],
            source_ref=prepared["ref"],
            source_payload=prepared["payload"],
            source_digest=_digest(json.dumps(prepared["payload"], ensure_ascii=False, sort_keys=True)),
            batch_id=batch_id,
            prompt_id=prompt["id"],
            prompt_version=prompt["version"],
            prompt_digest=prompt["digest"],
            prompt_text=prompt["text"],
            model=self.model_name(),
            status="queued",
        )

    def _skipped_generation(
        self, member: dict[str, Any], source: dict[str, Any], reason: str, batch_id: str
    ) -> dict[str, Any]:
        kind = str(source.get("kind") or "text")
        payload = {"skip": {"reason": reason, "requested": source}}
        generation = self.store.create_generation(
            member_id=_member_id(member),
            source_kind=kind,
            source_ref=str(source.get("ref") or source.get("filename") or source.get("text") or "")[:200],
            source_payload=payload,
            source_digest=_digest(reason),
            batch_id=batch_id,
            prompt_digest="",
            prompt_text="",
            model=self.model_name(),
            status="skipped",
        )
        return self.store.update_generation(
            generation["id"], stage="skipped", progress=100, skip_reason=reason
        )

    def start_history(
        self,
        member: dict[str, Any],
        *,
        since: str = "",
        until: str = "",
        limit: int = 10,
        include_merges: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        repo_root = self._repo_root(member)
        try:
            candidates = git_source.list_commits(
                repo_root,
                since=since,
                until=until,
                limit=max(1, min(int(limit), MAX_HISTORY_CANDIDATES)),
                include_merges=include_merges,
            )
        except git_source.AdrGitError as error:
            raise AdrServiceError(str(error), status=400) from error
        batch_id = _digest(f"{_member_id(member)}:{time.time()}")[7:23]
        generations = [
            self.create_generation(
                member,
                {"kind": "commit", "ref": commit["sha"]},
                force=force,
                batch_id=batch_id,
            )
            for commit in candidates
        ]
        return {"batch_id": batch_id, "total": len(generations), "generations": generations}

    def run(self, generation_id: str) -> dict[str, Any] | None:
        generation = self.store.get_generation(generation_id)
        if generation is None or generation["status"] not in {"queued", "running"}:
            return generation
        started = time.monotonic()
        self.store.update_generation(
            generation_id, status="running", stage="generating", progress=10, started_at=_now()
        )
        try:
            member = self.member(generation["member_id"])
            repo_root = self._repo_root(member)
            source_kind = generation["source_kind"]
            payload = generation["source_payload"] if isinstance(generation["source_payload"], dict) else {}
            if "skip" in payload:
                reason = str((payload.get("skip") or {}).get("reason") or "skipped")
                return self.store.update_generation(
                    generation_id,
                    status="skipped",
                    stage="skipped",
                    progress=100,
                    skip_reason=reason,
                    completed_at=_now(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

            values = self._prompt_values(member, repo_root, source_kind, generation["source_ref"], payload)
            prompt = render_prompt(generation["prompt_text"] or default_prompt(source_kind), values)
            self.store.update_generation(
                generation_id, stage="calling_model", progress=30, prompt_rendered=prompt
            )
            raw = self._complete(prompt)
            parsed = parse_json(raw)
            duration = int((time.monotonic() - started) * 1000)

            if not isinstance(parsed, dict) or "raw" in parsed:
                self.store.update_generation(
                    generation_id,
                    status="failed",
                    stage="parse_failed",
                    progress=100,
                    raw_response=raw[:200_000],
                    error_message="模型输出不是合法 JSON（原始响应已保留，便于调试提示词）",
                    duration_ms=duration,
                    completed_at=_now(),
                )
                return self.store.get_generation(generation_id)

            decision = normalize_decision(parsed)
            self.store.update_generation(
                generation_id,
                raw_response=raw[:200_000],
                parsed_json=json.dumps(decision, ensure_ascii=False),
                is_decision=1 if decision["is_decision"] else 0,
            )

            if not decision["is_decision"]:
                self.store.update_generation(
                    generation_id,
                    status="skipped",
                    stage="not_a_decision",
                    progress=100,
                    skip_reason=decision["reason"] or "模型判定输入不构成架构决策",
                    duration_ms=duration,
                    completed_at=_now(),
                )
                return self.store.get_generation(generation_id)

            service_name = _member_name(member)
            self._clean_evidence(decision, repo_root)
            title, slug, markdown = render_madr(
                decision,
                source_kind=source_kind,
                source_ref=generation["source_ref"],
                service=service_name,
            )
            draft = self.store.create_draft(
                generation_id=generation_id,
                member_id=generation["member_id"],
                source_kind=source_kind,
                source_ref=generation["source_ref"],
                title=title,
                slug=slug,
                markdown=markdown,
                status="proposed",
                confidence=decision.get("confidence"),
            )
            return self.store.update_generation(
                generation_id,
                status="succeeded",
                stage="draft_ready",
                progress=100,
                draft_id=draft["id"],
                duration_ms=duration,
                completed_at=_now(),
            )
        except AdrServiceError as error:
            self.store.update_generation(
                generation_id,
                status="failed",
                stage="failed",
                progress=100,
                error_message=str(error),
                duration_ms=int((time.monotonic() - started) * 1000),
                completed_at=_now(),
            )
            return self.store.get_generation(generation_id)
        except Exception as error:  # keep the trace; never lose the raw input
            logger.exception("ADR generation failed for %s", generation_id)
            self.store.update_generation(
                generation_id,
                status="failed",
                stage="failed",
                progress=100,
                error_message=f"{type(error).__name__}: {error}",
                duration_ms=int((time.monotonic() - started) * 1000),
                completed_at=_now(),
            )
            return self.store.get_generation(generation_id)

    def generate(self, member: dict[str, Any], source: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """Synchronous generation used by the CLI and single-shot callers."""
        generation = self.create_generation(member, source, force=force)
        return self.run(generation["id"]) or generation

    # ── reads ────────────────────────────────────────────────────────

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        generation = self.store.get_generation(generation_id)
        if generation is None:
            raise AdrServiceError("generation not found", status=404)
        draft = self.store.get_draft_for_generation(generation_id)
        return {"generation": generation, "draft": draft}

    def list_generations(self, member_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_generations(member_id=member_id, limit=limit)

    def batch_progress(self, batch_id: str) -> dict[str, Any]:
        generations = self.store.list_generations(batch_id=batch_id, limit=200)
        counts = self.store.count_generations(batch_id)
        return {"batch_id": batch_id, "counts": counts, "generations": generations}

    def source_mapping(self, member_id: str) -> dict[str, Any]:
        """Show how each source kind maps to its prompt and latest ADR."""
        prompts = {item["source_kind"]: item for item in self.prompts(member_id)["kinds"]}
        generations = self.store.list_generations(member_id=member_id, limit=200)
        drafts = self.store.list_drafts(member_id, limit=200)
        latest_generation: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        for generation in generations:
            kind = generation["source_kind"]
            counts[kind] = counts.get(kind, 0) + 1
            latest_generation.setdefault(kind, generation)
        latest_draft: dict[str, dict[str, Any]] = {}
        for draft in drafts:
            latest_draft.setdefault(draft["source_kind"], draft)

        rows = []
        for kind in PROMPT_SOURCE_KINDS:
            item = prompts.get(kind, {})
            prompt = item.get("prompt")
            generation = latest_generation.get(kind)
            draft = latest_draft.get(kind)
            rows.append(
                {
                    "source_kind": kind,
                    "prompt_origin": "member" if prompt else "default",
                    "prompt_version": prompt["version"] if prompt else 0,
                    "prompt_digest": prompt["prompt_digest"] if prompt else item.get("prompt_digest", ""),
                    "generation_count": counts.get(kind, 0),
                    "latest_generation": (
                        {
                            "id": generation["id"],
                            "status": generation["status"],
                            "source_ref": generation["source_ref"],
                            "prompt_version": generation["prompt_version"],
                            "created_at": generation["created_at"],
                        }
                        if generation
                        else None
                    ),
                    "latest_draft": (
                        {
                            "id": draft["id"],
                            "title": draft["title"],
                            "status": draft["status"],
                            "exported_path": draft["exported_path"],
                        }
                        if draft
                        else None
                    ),
                }
            )
        return {"member_id": member_id, "sources": rows}

    def list_drafts(self, member_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_drafts(member_id, limit)

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        generation = self.store.get_generation(draft["generation_id"])
        return {"draft": draft, "generation": generation}

    # ── export ───────────────────────────────────────────────────────

    def export_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        member = self.member(draft["member_id"])
        repo_root = self._repo_root(member)
        directory = next(
            (repo_root / candidate for candidate in EXPORT_DIR_CANDIDATES if (repo_root / candidate).is_dir()),
            repo_root / "docs" / "adr",
        )
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9-]+", "-", draft["slug"].lower()).strip("-") or "decision"
        target = None
        if draft.get("exported_path"):
            candidate = (repo_root / draft["exported_path"]).resolve()
            try:
                candidate.relative_to(directory.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                target = candidate
        if target is None:
            numbers = []
            for path in directory.glob("*.md"):
                match = FILENAME_RE.match(path.name)
                if match:
                    numbers.append(int(match.group("number")))
            number = (max(numbers) + 1) if numbers else 1
            target = directory / f"{number:04d}-{slug}.md"
            while target.exists():
                number += 1
                target = directory / f"{number:04d}-{slug}.md"
        target.write_text(draft["markdown"].rstrip("\n") + "\n", encoding="utf-8")
        index_path = directory / "index.md"
        index_path.write_text(render_index(load_adrs(directory)), encoding="utf-8")
        relative = target.relative_to(repo_root).as_posix()
        updated = self.store.mark_exported(draft_id, relative)
        return {
            "draft": updated,
            "path": str(target),
            "relative_path": relative,
            "index_path": str(index_path),
        }


class AdrScheduler:
    """Bounded in-process execution; state lives in SQLite for recovery."""

    def __init__(self, service: AdrService, *, workers: int = 2):
        self.service = service
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="adr")
        self.futures: dict[str, Any] = {}

    def start(self) -> None:
        self.service.store.recover_interrupted()
        for generation in self.service.list_generations(limit=200):
            if generation["status"] == "queued":
                self.submit(generation["id"])

    def submit(self, generation_id: str):
        future = self.futures.get(generation_id)
        if future is not None and not future.done():
            return future
        future = self.executor.submit(self.service.run, generation_id)
        self.futures[generation_id] = future
        return future

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


__all__ = [
    "AdrScheduler",
    "AdrService",
    "AdrServiceError",
    "EXPORT_DIR_CANDIDATES",
    "MAX_FILE_CHARS",
    "MAX_TEXT_CHARS",
]
