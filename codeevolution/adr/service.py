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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..analysis.knowledge.adr import FILENAME_RE, load_adrs, render_index
from ..semantic.config import get_llm_config
from ..semantic.json_parser import parse_json
from ..text_encoding import decode_text_bytes
from . import git_source
from .domain import (
    DEFAULT_ADR_TEMPLATE,
    GENERATION_KINDS,
    MAX_TITLE_CHARS,
    PROMPT_SOURCE_KINDS,
    decision_schema_text,
    default_prompt,
    inject_provenance,
    normalize_decision,
    prompt_digest,
    render_madr,
    render_prompt,
    validate_template,
)
from .store import AdrStore

logger = logging.getLogger(__name__)

MAX_FILE_CHARS = 120_000
MAX_TEXT_CHARS = 20_000
MAX_DRAFT_CHARS = 200_000
MAX_HISTORY_CANDIDATES = 100
MAX_COMMIT_PAGE = 100
# The commit table shows the whole history, so its status/progress needs every
# commit's changed files.  That index is one ``git log`` per (repo, HEAD, range);
# beyond this size the request asks the user to narrow the date range instead of
# silently reporting a progress number that only covers part of the history.
MAX_INDEXED_COMMITS = 20_000
COMMIT_INDEX_TTL_SECONDS = 30
TRIAGE_BATCH_SIZE = 12
BACKTEST_VERDICTS = ("consistent", "partial", "contradicted", "unverifiable")
COMMIT_STAGES = (
    "pending",
    "queued",
    "generating",
    "generated",
    "backtested",
    "approved",
    "rejected",
    "exported",
    "skipped",
    "failed",
)
EXPORT_DIR_CANDIDATES = ("docs/adr", "doc/adr", "docs/decisions", "adr")
# BOM-marked UTF-8 keeps exported ADRs readable in Windows editors that would
# otherwise guess GBK; the readers in this project strip it again.
EXPORT_ENCODING = "utf-8-sig"


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
        # Full-history commit index shared by the table, the status chips and the
        # progress bar; invalidated by HEAD moving or the TTL expiring.
        self._commit_index: dict[tuple[str, str, str, str, bool], tuple[float, list[dict[str, Any]]]] = {}
        self._commit_index_lock = threading.Lock()

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
            content, encoding = decode_text_bytes(candidate.read_bytes())
        except OSError as error:
            raise AdrServiceError(f"cannot read file: {relative}") from error
        if encoding not in {"utf-8", "utf-8-sig"}:
            logger.warning("ADR source file decoded as %s: %s", encoding, relative)
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

    # ── output template ──────────────────────────────────────────────

    def template(self, member_id: str) -> dict[str, Any]:
        current = self.store.current_template(member_id)
        return {
            "member_id": member_id,
            "template": current,
            "default_template": DEFAULT_ADR_TEMPLATE,
            "template_digest": current["template_digest"] if current else _digest(DEFAULT_ADR_TEMPLATE),
            "template_version": current["version"] if current else 0,
        }

    def save_template(self, member_id: str, template_text: str) -> dict[str, Any]:
        try:
            text = validate_template(template_text)
        except ValueError as error:
            raise AdrServiceError(str(error)) from error
        return self.store.create_template(member_id, text, _digest(text))

    def reset_template(self, member_id: str) -> bool:
        return self.store.delete_templates(member_id)

    def _render_template(self, member_id: str) -> str:
        current = self.store.current_template(member_id)
        return current["template_text"] if current else DEFAULT_ADR_TEMPLATE

    # ── commit table + AI triage ─────────────────────────────────────

    @staticmethod
    def _commit_stage(candidate: bool, draft: dict[str, Any] | None, generation: dict[str, Any] | None) -> str:
        if draft:
            if draft.get("exported_path"):
                return "exported"
            review = draft.get("review_status")
            if review == "approved":
                return "approved"
            if review == "rejected":
                return "rejected"
            backtest = draft.get("backtest") or {}
            if backtest.get("status") == "ready":
                return "backtested"
            return "generated"
        if generation:
            status = generation.get("status")
            if status == "queued":
                return "queued"
            if status == "running":
                return "generating"
            if status == "failed":
                return "failed"
            if status == "skipped":
                return "skipped"
        return "pending" if candidate else "skipped"

    def _scan_item(
        self,
        commit: dict[str, Any],
        scan: dict[str, Any] | None,
        draft: dict[str, Any] | None,
        generation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        rule_candidate = bool(commit["significant"])
        if scan is None:
            if rule_candidate:
                candidate, reason = True, ""
            else:
                candidate, reason = False, "仅修改文档、测试或 lockfile"
            ai_candidate = None
            change_type = ""
            scanned = False
            rule_candidate_value = rule_candidate
        else:
            ai_candidate = scan.get("ai_candidate")
            rule_candidate_value = bool(scan.get("rule_candidate"))
            if not rule_candidate_value:
                candidate, reason = False, scan.get("rule_reason") or "仅修改文档、测试或 lockfile"
            elif ai_candidate is None:
                candidate, reason = True, scan.get("ai_reason") or "AI 分诊未完成，可手动生成"
            else:
                candidate, reason = bool(ai_candidate), scan.get("ai_reason", "")
            change_type = scan.get("change_type", "")
            scanned = True

        stage = self._commit_stage(candidate, draft, generation)
        backtest = (draft or {}).get("backtest") or {}
        return {
            "sha": commit["sha"],
            "subject": commit["subject"],
            "author": commit["author"],
            "committed_at": commit["committed_at"],
            "files": commit["files"],
            "rule_candidate": rule_candidate_value,
            "ai_candidate": ai_candidate,
            "candidate": candidate,
            "skip_reason": reason,
            "change_type": change_type,
            "scanned": scanned,
            "triaged": bool(scan is not None and scan.get("ai_candidate") is not None),
            "stage": stage,
            "generation_id": generation["id"] if generation else "",
            "generation_status": generation.get("status", "") if generation else "",
            "generation_stage": generation.get("stage", "") if generation else "",
            "generation_progress": generation.get("progress", 0) if generation else 0,
            "draft_id": draft["id"] if draft else "",
            "draft_title": draft["title"] if draft else "",
            "draft_status": draft["status"] if draft else "",
            "review_status": draft.get("review_status") if draft else "",
            "rating": draft.get("rating") if draft else None,
            "exported_path": draft.get("exported_path") if draft else "",
            "backtest": (
                {
                    "verdict": backtest.get("verdict", ""),
                    "confidence": backtest.get("confidence"),
                    "status": backtest.get("status", ""),
                }
                if backtest
                else None
            ),
        }

    def _history_commits(
        self,
        repo_root: Path,
        *,
        since: str,
        until: str,
        include_merges: bool,
        total: int,
    ) -> list[dict[str, Any]]:
        """Every commit in range with its changed files, cached per HEAD.

        The table, the status chips and the progress bar all need the full
        history; reading it once and caching it turns page navigation into an
        in-memory slice instead of one ``git show`` per row.
        """
        key = (str(repo_root), git_source.head_commit(repo_root), since, until, bool(include_merges))
        now = time.monotonic()
        with self._commit_index_lock:
            cached = self._commit_index.get(key)
            if cached is not None and now - cached[0] < COMMIT_INDEX_TTL_SECONDS:
                return cached[1]
        try:
            commits = git_source.list_commits(
                repo_root,
                since=since,
                until=until,
                limit=max(1, total),
                offset=0,
                include_merges=include_merges,
            )
        except git_source.AdrGitError as error:
            raise AdrServiceError(str(error), status=400) from error
        with self._commit_index_lock:
            self._commit_index[key] = (now, commits)
            if len(self._commit_index) > 4:
                stale_keys = sorted(
                    self._commit_index, key=lambda item: self._commit_index[item][0]
                )[:-4]
                for stale in stale_keys:
                    self._commit_index.pop(stale, None)
        return commits

    def list_commits(
        self,
        member: Any,
        *,
        since: str = "",
        until: str = "",
        offset: int = 0,
        limit: int = 50,
        include_merges: bool = False,
        stage: str = "",
    ) -> dict[str, Any]:
        """One page of the full history plus global status counts and progress.

        ``total`` is the number of commits matching ``stage`` (the page count
        basis); ``total_all`` and ``progress`` always describe the whole range,
        so the progress bar does not change when the user pages or filters.
        ``processed`` counts every commit that left ``pending`` (drafted,
        triaged-skip or later) but not queued/generating ones.
        """
        member_id = _member_id(member)
        if stage and stage not in COMMIT_STAGES:
            raise AdrServiceError(f"unknown commit stage: {stage}")
        repo_root = self._repo_root(member)
        try:
            total_all = git_source.count_commits(
                repo_root, since=since, until=until, include_merges=include_merges
            )
        except git_source.AdrGitError as error:
            raise AdrServiceError(str(error), status=400) from error
        if total_all > MAX_INDEXED_COMMITS:
            raise AdrServiceError(
                f"仓库历史共 {total_all} 个提交，超过 {MAX_INDEXED_COMMITS} 个上限；"
                "请用起始/结束日期缩小范围后再查看提交表",
                status=400,
            )
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_COMMIT_PAGE))
        commits = (
            self._history_commits(
                repo_root,
                since=since,
                until=until,
                include_merges=include_merges,
                total=total_all,
            )
            if total_all
            else []
        )
        scans = {scan["sha"]: scan for scan in self.store.list_scans(member_id)}
        drafts = {
            draft["source_ref"]: draft
            for draft in self.list_drafts(member_id, limit=500)
            if draft["source_kind"] == "commit"
        }
        latest_generation: dict[str, dict[str, Any]] = {}
        for generation in self.store.list_generations(member_id=member_id, limit=500):
            if generation["source_kind"] != "commit":
                continue
            latest_generation.setdefault(generation["source_ref"], generation)
        items = [
            self._scan_item(
                commit,
                scans.get(commit["sha"]),
                drafts.get(commit["sha"]),
                latest_generation.get(commit["sha"]),
            )
            for commit in commits
        ]
        counts = {stage_name: 0 for stage_name in COMMIT_STAGES}
        for item in items:
            counts[item["stage"]] = counts.get(item["stage"], 0) + 1
        processed = sum(
            counts.get(name, 0)
            for name in ("generated", "backtested", "approved", "rejected", "exported", "skipped", "failed")
        )
        active = counts.get("queued", 0) + counts.get("generating", 0)
        visible = [item for item in items if not stage or item["stage"] == stage]
        page = visible[offset : offset + limit]
        return {
            "member_id": member_id,
            "repo": str(repo_root),
            "items": page,
            "total": len(visible),
            "total_all": len(items),
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "has_more": offset + len(page) < len(visible),
            "scanned": sum(1 for item in items if item["scanned"]),
            "counts": counts,
            "stage": stage,
            "progress": {
                "total": len(items),
                "processed": processed,
                "active": active,
                "percent": int(round(processed * 100 / len(items))) if items else 0,
            },
        }

    def scan_commits(
        self,
        member: Any,
        *,
        since: str = "",
        until: str = "",
        offset: int = 0,
        limit: int = 50,
        include_merges: bool = False,
        refresh: bool = False,
        stage: str = "",
    ) -> dict[str, Any]:
        """Rule-filter obvious noise, then ask the model to triage the page."""
        member_id = _member_id(member)
        repo_root = self._repo_root(member)
        listing = self.list_commits(
            member,
            since=since,
            until=until,
            offset=offset,
            limit=limit,
            include_merges=include_merges,
            stage=stage,
        )
        pending = [
            item for item in listing["items"] if refresh or not item["triaged"]
        ]
        for item in pending:
            if not item["rule_candidate"]:
                self.store.upsert_scan(
                    {
                        "member_id": member_id,
                        "sha": item["sha"],
                        "subject": item["subject"],
                        "author": item["author"],
                        "committed_at": item["committed_at"],
                        "files": item["files"],
                        "rule_candidate": False,
                        "rule_reason": "仅修改文档、测试或 lockfile",
                        "ai_candidate": False,
                        "ai_reason": "规则过滤：仅修改文档、测试或 lockfile",
                        "change_type": "docs",
                        "model": "",
                        "prompt_digest": "",
                    }
                )

        candidates = [item for item in pending if item["rule_candidate"]]
        prompt = self._resolve_prompt(member_id, "triage")
        failures = 0
        for start in range(0, len(candidates), TRIAGE_BATCH_SIZE):
            chunk = candidates[start : start + TRIAGE_BATCH_SIZE]
            values = {
                "service": _member_name(member),
                "repo": str(repo_root),
                "commits": json.dumps(
                    [
                        {
                            "sha": item["sha"],
                            "subject": item["subject"],
                            "author": item["author"],
                            "date": item["committed_at"],
                            "files": item["files"][:20],
                        }
                        for item in chunk
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
            }
            rendered = render_prompt(prompt["text"], values)
            try:
                raw = self._complete(rendered)
                parsed = parse_json(raw)
                decisions = self._parse_triage(parsed, [item["sha"] for item in chunk])
                if not decisions:
                    raise AdrServiceError("模型没有返回可解析的分诊结果", status=502)
                for item in chunk:
                    decision = decisions.get(item["sha"])
                    if decision is None:
                        self.store.upsert_scan(
                            {
                                "member_id": member_id,
                                "sha": item["sha"],
                                "subject": item["subject"],
                                "author": item["author"],
                                "committed_at": item["committed_at"],
                                "files": item["files"],
                                "rule_candidate": True,
                                "ai_candidate": None,
                                "ai_reason": "模型未返回该提交的分诊结果，可手动生成",
                                "change_type": "unknown",
                                "model": self.model_name(),
                                "prompt_digest": prompt["digest"],
                            }
                        )
                        continue
                    self.store.upsert_scan(
                        {
                            "member_id": member_id,
                            "sha": item["sha"],
                            "subject": item["subject"],
                            "author": item["author"],
                            "committed_at": item["committed_at"],
                            "files": item["files"],
                            "rule_candidate": True,
                            "ai_candidate": bool(decision.get("candidate")),
                            "ai_reason": str(decision.get("reason") or ""),
                            "change_type": str(decision.get("change_type") or "unknown"),
                            "model": self.model_name(),
                            "prompt_digest": prompt["digest"],
                        }
                    )
            except (AdrServiceError, Exception) as error:
                failures += len(chunk)
                logger.warning("ADR commit triage failed for %s: %s", member_id, error)
                for item in chunk:
                    self.store.upsert_scan(
                        {
                            "member_id": member_id,
                            "sha": item["sha"],
                            "subject": item["subject"],
                            "author": item["author"],
                            "committed_at": item["committed_at"],
                            "files": item["files"],
                            "rule_candidate": True,
                            "ai_candidate": None,
                            "ai_reason": f"AI 分诊失败：{error}",
                            "change_type": "unknown",
                            "model": self.model_name(),
                            "prompt_digest": prompt["digest"],
                        }
                    )

        result = self.list_commits(
            member,
            since=since,
            until=until,
            offset=offset,
            limit=limit,
            include_merges=include_merges,
            stage=stage,
        )
        result["failures"] = failures
        return result

    @staticmethod
    def _parse_triage(parsed: Any, shas: list[str]) -> dict[str, dict[str, Any]]:
        rows: list[Any] = []
        if isinstance(parsed, list):
            rows = parsed
        elif isinstance(parsed, dict):
            for key in ("commits", "items", "results"):
                value = parsed.get(key)
                if isinstance(value, list):
                    rows = value
                    break
            if not rows and any(sha[:8] in json.dumps(parsed) for sha in shas):
                rows = [parsed]
        wanted = {sha for sha in shas}
        decisions: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sha = str(row.get("sha") or "").strip()
            if sha in wanted:
                decisions[sha] = row
                continue
            for candidate_sha in wanted:
                if sha and candidate_sha.startswith(sha[:8]):
                    decisions[candidate_sha] = row
                    break
        return decisions

    def batch_generate(
        self, member: Any, shas: list[str], *, force: bool = False
    ) -> dict[str, Any]:
        commit_shas = [str(sha).strip() for sha in shas if str(sha).strip()][:100]
        if not commit_shas:
            raise AdrServiceError("at least one commit is required")
        batch_id = "batch-" + _digest(f"{_member_id(member)}:{time.time()}")[7:23]
        generations = [
            self.create_generation(
                member, {"kind": "commit", "ref": sha}, force=force, batch_id=batch_id
            )
            for sha in commit_shas
        ]
        return {
            "batch_id": batch_id,
            "total": len(generations),
            "generations": generations,
            "progress": self.batch_progress(batch_id)["progress"],
        }

    # ── backtest ─────────────────────────────────────────────────────

    def backtest(self, draft_id: str, *, force: bool = False) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        existing = self.store.get_backtest(draft_id)
        if existing is not None and not force:
            return existing
        member = self.member(draft["member_id"])
        repo_root = self._repo_root(member)
        generation = self.store.get_generation(draft["generation_id"]) or {}
        prompt = self._resolve_prompt(draft["member_id"], "backtest")
        code = self._backtest_evidence(repo_root, generation)
        rendered = render_prompt(
            prompt["text"],
            {
                "service": _member_name(member),
                "repo": str(repo_root),
                "adr": draft["markdown"],
                "code": code,
            },
        )
        started = time.monotonic()
        try:
            raw = self._complete(rendered)
        except AdrServiceError as error:
            record = self.store.upsert_backtest(
                {
                    "draft_id": draft_id,
                    "status": "failed",
                    "model": self.model_name(),
                    "prompt_text": prompt["text"],
                    "prompt_rendered": rendered,
                    "error_message": str(error),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return record
        duration = int((time.monotonic() - started) * 1000)
        parsed = parse_json(raw)
        if not isinstance(parsed, dict) or "raw" in parsed:
            return self.store.upsert_backtest(
                {
                    "draft_id": draft_id,
                    "status": "failed",
                    "model": self.model_name(),
                    "prompt_text": prompt["text"],
                    "prompt_rendered": rendered,
                    "raw_response": raw[:200_000],
                    "error_message": "模型输出不是合法 JSON",
                    "duration_ms": duration,
                }
            )
        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in BACKTEST_VERDICTS:
            verdict = "unverifiable"
        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
        evidence = parsed.get("evidence") if isinstance(parsed.get("evidence"), list) else []
        return self.store.upsert_backtest(
            {
                "draft_id": draft_id,
                "status": "ready",
                "verdict": verdict,
                "confidence": confidence,
                "reasons": [str(item) for item in reasons],
                "evidence": [item for item in evidence if isinstance(item, dict)],
                "recommendation": str(parsed.get("recommendation") or ""),
                "model": self.model_name(),
                "prompt_text": prompt["text"],
                "prompt_rendered": rendered,
                "raw_response": raw[:200_000],
                "duration_ms": duration,
            }
        )

    def _backtest_evidence(self, repo_root: Path, generation: dict[str, Any]) -> str:
        parsed = generation.get("parsed") if isinstance(generation.get("parsed"), dict) else {}
        paths: list[str] = []
        for item in parsed.get("evidence") or []:
            path = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
            if path and path not in paths:
                paths.append(path)
        parts: list[str] = []
        for path in paths[:5]:
            try:
                relative, content = self._read_repo_file(repo_root, path)
            except AdrServiceError:
                continue
            parts.append(f"### {relative}\n```\n{content[:8000]}\n```")
        files = git_source.current_files(repo_root, limit=120)
        if files:
            parts.append("仓库文件（部分）：\n" + "\n".join(f"* {item}" for item in files))
        subjects = git_source.recent_subjects(repo_root, limit=8)
        if subjects:
            parts.append("最近提交：\n" + "\n".join(f"* {item}" for item in subjects))
        return "\n\n".join(parts)[:30_000] or "（仓库没有可用证据）"

    # ── human review ─────────────────────────────────────────────────

    def review(
        self,
        draft_id: str,
        *,
        action: str,
        rating: int | None = None,
        comment: str = "",
        reviewer: str = "",
    ) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        if draft.get("exported_path"):
            raise AdrServiceError(
                "已导出的 ADR 不能再审批；如需修改请重新生成草稿", status=409
            )
        if action not in {"approve", "reject"}:
            raise AdrServiceError("action must be approve or reject")
        if rating is not None and not 1 <= int(rating) <= 5:
            raise AdrServiceError("rating must be between 1 and 5")
        status = "accepted" if action == "approve" else "rejected"
        markdown = self._set_status(draft["markdown"], status)
        updated = self.store.update_draft(
            draft_id,
            status=status,
            markdown=markdown,
            review_status="approved" if action == "approve" else "rejected",
            rating=int(rating) if rating is not None else draft.get("rating"),
            review_comment=comment,
            reviewer=(reviewer or "").strip() or draft.get("reviewer", ""),
            reviewed_at=_now(),
        )
        return {"draft": updated, "backtest": self.store.get_backtest(draft_id)}

    def edit_draft(
        self,
        draft_id: str,
        *,
        markdown: str,
        title: str = "",
        editor: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Store an edited draft as a new revision and invalidate the review.

        Refinement is a first-class step: a human (or an external agent) may
        rewrite the model draft, but the result is a new revision that must be
        reviewed again before export. Already-exported records are immutable
        here — the repository file is the source of truth from that point on.
        """
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        if draft.get("exported_path"):
            raise AdrServiceError(
                "已导出的 ADR 不能再编辑；如需修改请重新生成草稿", status=409
            )
        text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text.strip():
            raise AdrServiceError("ADR 内容不能为空")
        if len(text) > MAX_DRAFT_CHARS:
            raise AdrServiceError(f"ADR 内容不能超过 {MAX_DRAFT_CHARS} 字符")
        if not text.lstrip().startswith("---"):
            text = f"---\nstatus: proposed\n---\n\n{text}"
        text = self._set_status(text.rstrip("\n") + "\n", "proposed")
        new_title = (title or "").strip()[:MAX_TITLE_CHARS] or self._draft_title(text) or draft["title"]
        editor_name = (editor or "").strip()[:200]
        current_revision = int(draft.get("revision") or 1)
        existing = {int(item["revision"]) for item in self.store.list_revisions(draft_id, limit=500)}
        if current_revision not in existing:
            # Drafts created before revisions existed: preserve their text first.
            self.store.add_revision(
                draft_id,
                revision=current_revision,
                markdown=draft["markdown"],
                title=draft["title"],
                editor=draft.get("editor", ""),
                note="编辑前版本",
            )
        new_revision = current_revision + 1
        self.store.add_revision(
            draft_id,
            revision=new_revision,
            markdown=text,
            title=new_title,
            editor=editor_name,
            note=(note or "").strip()[:2000],
        )
        updated = self.store.update_draft(
            draft_id,
            markdown=text,
            title=new_title,
            revision=new_revision,
            status="proposed",
            review_status="pending",
            rating=None,
            review_comment="",
            reviewed_at=None,
            reviewer="",
            editor=editor_name,
            edited_at=_now(),
        )
        # A backtest describes the previous text; require a fresh run.
        self.store.delete_backtest_for_draft(draft_id)
        return {
            "draft": updated,
            "revisions": self.store.list_revisions(draft_id),
            "backtest": None,
        }

    @staticmethod
    def _draft_title(markdown: str) -> str:
        match = re.search(r"(?m)^#\s+(.+)$", markdown or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def _set_status(markdown: str, status: str) -> str:
        if re.search(r"(?m)^status:\s*.*$", markdown):
            return re.sub(r"(?m)^status:\s*.*$", f"status: {status}", markdown, count=1)
        if markdown.startswith("# "):
            return f"---\nstatus: {status}\n---\n\n{markdown}"
        return markdown

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
        if prepared["kind"] == "commit" and not force:
            scan = self.store.get_scan(member_key, prepared["ref"])
            if scan is not None and scan.get("ai_candidate") is False:
                return self._skipped_generation(
                    member,
                    source,
                    f"AI 分诊跳过：{scan.get('ai_reason') or '判定不是架构决策'}",
                    batch_id,
                )
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
        return {
            "batch_id": batch_id,
            "total": len(generations),
            "generations": generations,
            "progress": self.batch_progress(batch_id)["progress"],
        }

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
                template=self._render_template(generation["member_id"]),
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
        """Mirror the API-explanation batch contract: progress over the selection.

        ``completed`` counts generations that finished executing (succeeded);
        ``failed``/``skipped`` are terminal too, so ``percent`` is the share of
        selected commits that no longer need work.
        """
        generations = self.store.list_generations(batch_id=batch_id, limit=200)
        counts = self.store.count_generations(batch_id)
        total = sum(counts.values())
        completed = counts.get("succeeded", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        running = counts.get("running", 0)
        queued = counts.get("queued", 0)
        done = completed + failed + skipped
        progress = {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "running": running,
            "queued": queued,
            "percent": round(done * 100 / total) if total else 100,
        }
        return {
            "batch_id": batch_id,
            "counts": counts,
            "progress": progress,
            "generations": generations,
        }

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
        for kind in GENERATION_KINDS:
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
        drafts = self.store.list_drafts(member_id, limit)
        for draft in drafts:
            backtest = self.store.get_backtest(draft["id"])
            draft["backtest"] = (
                {
                    "verdict": backtest["verdict"],
                    "confidence": backtest["confidence"],
                    "status": backtest["status"],
                }
                if backtest
                else None
            )
        return drafts

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        generation = self.store.get_generation(draft["generation_id"])
        return {
            "draft": draft,
            "generation": generation,
            "backtest": self.store.get_backtest(draft_id),
            "revisions": self.store.list_revisions(draft_id),
        }

    # ── export ───────────────────────────────────────────────────────

    def _export_provenance(
        self, draft: dict[str, Any], generation: dict[str, Any] | None
    ) -> dict[str, Any]:
        generation = generation or {}
        parsed = generation.get("parsed") if isinstance(generation.get("parsed"), dict) else {}
        evidence = [
            str(item.get("path"))
            for item in (parsed.get("evidence") or [])
            if isinstance(item, dict) and item.get("path")
        ]
        return {
            "tool": "codeevolution",
            "draft_id": draft["id"],
            "generation_id": draft.get("generation_id", ""),
            "member_id": draft.get("member_id", ""),
            "source": f'{draft.get("source_kind", "")}:{draft.get("source_ref", "")}'.rstrip(":"),
            "model": generation.get("model") or self.model_name(),
            "prompt_digest": generation.get("prompt_digest", ""),
            "revision": draft.get("revision") or 1,
            "evidence": evidence[:20],
            "editor": draft.get("editor", ""),
            "reviewer": draft.get("reviewer", ""),
            "reviewed_at": draft.get("reviewed_at") or "",
            "exported_at": _now(),
        }

    def export_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise AdrServiceError("draft not found", status=404)
        if draft.get("review_status") != "approved" or draft.get("status") != "accepted":
            raise AdrServiceError("ADR 草稿需先通过人工审批（accepted）才能导出", status=409)
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
        generation = self.store.get_generation(draft["generation_id"])
        markdown = inject_provenance(
            draft["markdown"], self._export_provenance(draft, generation)
        )
        # UTF-8 BOM so Windows Notepad, Explorer preview and Word detect the
        # encoding instead of falling back to the ANSI/GBK code page. Readers
        # in this project (and PyYAML/MADR tooling) tolerate the BOM.
        target.write_text(markdown.rstrip("\n") + "\n", encoding=EXPORT_ENCODING)
        index_path = directory / "index.md"
        index_path.write_text(
            render_index(load_adrs(directory)), encoding=EXPORT_ENCODING
        )
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
