"""ADR generation storage: per-service prompts, generation traces and drafts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adr_prompts (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    prompt_digest TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adr_prompts_scope
    ON adr_prompts(member_id, source_kind, version);

CREATE TABLE IF NOT EXISTS adr_generations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL DEFAULT '',
    member_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL DEFAULT '',
    source_payload TEXT NOT NULL DEFAULT '{}',
    prompt_id TEXT NOT NULL DEFAULT '',
    prompt_version INTEGER NOT NULL DEFAULT 0,
    prompt_digest TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    prompt_rendered TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    is_decision INTEGER,
    skip_reason TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    parsed_json TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    draft_id TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_adr_generations_member
    ON adr_generations(member_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_adr_generations_batch
    ON adr_generations(batch_id);

CREATE TABLE IF NOT EXISTS adr_drafts (
    id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL UNIQUE,
    member_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    markdown TEXT NOT NULL,
    confidence REAL,
    exported_path TEXT NOT NULL DEFAULT '',
    exported_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adr_drafts_member
    ON adr_drafts(member_id, created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _synchronized(method):
    """Serialize access to the shared SQLite connection across scheduler threads."""

    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    return wrapper


class AdrStore:
    """SQLite-backed ADR generation state, independent from snapshot facts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(adr_generations)")
        }
        if "prompt_rendered" not in columns:
            with self._connection:
                self._connection.execute(
                    "ALTER TABLE adr_generations ADD COLUMN prompt_rendered TEXT NOT NULL DEFAULT ''"
                )

    def close(self) -> None:
        self._connection.close()

    # ── prompts ──────────────────────────────────────────────────────

    @_synchronized
    def create_prompt(self, member_id: str, source_kind: str, prompt_text: str, digest: str) -> dict[str, Any]:
        text = (prompt_text or "").strip()
        if not text:
            raise ValueError("prompt text is required")
        current = self.current_prompt(member_id, source_kind)
        version = (current["version"] + 1) if current else 1
        prompt_id = str(uuid.uuid4())
        created_at = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO adr_prompts
                   (id, member_id, source_kind, prompt_text, prompt_digest, version, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (prompt_id, member_id, source_kind, text, digest, version, created_at),
            )
        return {
            "id": prompt_id,
            "member_id": member_id,
            "source_kind": source_kind,
            "prompt_text": text,
            "prompt_digest": digest,
            "version": version,
            "created_at": created_at,
        }

    @_synchronized
    def current_prompt(self, member_id: str, source_kind: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """SELECT * FROM adr_prompts
               WHERE member_id=? AND source_kind=?
               ORDER BY version DESC LIMIT 1""",
            (member_id, source_kind),
        ).fetchone()
        return _row(row)

    @_synchronized
    def list_current_prompts(self, member_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT p.* FROM adr_prompts p
               JOIN (SELECT source_kind, MAX(version) AS version
                     FROM adr_prompts WHERE member_id=? GROUP BY source_kind) latest
                 ON p.source_kind=latest.source_kind AND p.version=latest.version
               WHERE p.member_id=?
               ORDER BY p.source_kind""",
            (member_id, member_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def prompt_history(self, member_id: str, source_kind: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT * FROM adr_prompts WHERE member_id=? AND source_kind=?
               ORDER BY version DESC LIMIT ?""",
            (member_id, source_kind, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def delete_prompts(self, member_id: str, source_kind: str) -> bool:
        """Drop every stored version so the built-in default applies again."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM adr_prompts WHERE member_id=? AND source_kind=?",
                (member_id, source_kind),
            )
        return cursor.rowcount > 0

    # ── generations ──────────────────────────────────────────────────

    @_synchronized
    def create_generation(
        self,
        *,
        member_id: str,
        source_kind: str,
        source_ref: str,
        source_payload: dict[str, Any],
        source_digest: str,
        batch_id: str = "",
        prompt_id: str = "",
        prompt_version: int = 0,
        prompt_digest: str = "",
        prompt_text: str = "",
        prompt_rendered: str = "",
        model: str = "",
        status: str = "queued",
    ) -> dict[str, Any]:
        generation_id = str(uuid.uuid4())
        created_at = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO adr_generations
                   (id, batch_id, member_id, source_kind, source_ref, source_digest,
                    source_payload, prompt_id, prompt_version, prompt_digest, prompt_text,
                    prompt_rendered, model, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    batch_id,
                    member_id,
                    source_kind,
                    source_ref,
                    source_digest,
                    json.dumps(source_payload, ensure_ascii=False),
                    prompt_id,
                    prompt_version,
                    prompt_digest,
                    prompt_text,
                    prompt_rendered,
                    model,
                    status,
                    created_at,
                ),
            )
        return self.get_generation(generation_id)  # type: ignore[return-value]

    @_synchronized
    def update_generation(self, generation_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "stage",
            "progress",
            "is_decision",
            "skip_reason",
            "raw_response",
            "parsed_json",
            "error_message",
            "draft_id",
            "duration_ms",
            "started_at",
            "completed_at",
            "prompt_id",
            "prompt_version",
            "prompt_digest",
            "prompt_text",
            "prompt_rendered",
            "model",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_generation(generation_id)
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connection:
            self._connection.execute(
                f"UPDATE adr_generations SET {assignments} WHERE id=?",
                (*updates.values(), generation_id),
            )
        return self.get_generation(generation_id)

    @_synchronized
    def get_generation(self, generation_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_generations WHERE id=?", (generation_id,)
        ).fetchone()
        return self._decode_generation(row)

    @_synchronized
    def list_generations(
        self,
        *,
        member_id: str = "",
        batch_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if member_id:
            clauses.append("member_id=?")
            params.append(member_id)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM adr_generations{where} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 200))),
        ).fetchall()
        return [decoded for row in rows if (decoded := self._decode_generation(row))]

    @_synchronized
    def count_generations(self, batch_id: str) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM adr_generations WHERE batch_id=? GROUP BY status",
            (batch_id,),
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    @_synchronized
    def recover_interrupted(self) -> int:
        """Mark generations left running by a restart as failed."""
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE adr_generations
                   SET status='failed', stage='interrupted',
                       error_message='generation interrupted by service restart',
                       completed_at=?
                   WHERE status IN ('queued','running')""",
                (_now(),),
            )
        return cursor.rowcount

    @staticmethod
    def _decode_generation(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["source_payload"] = _loads(data.get("source_payload"), {})
        data["parsed"] = _loads(data.get("parsed_json"), None)
        return data

    # ── drafts ───────────────────────────────────────────────────────

    @_synchronized
    def create_draft(
        self,
        *,
        generation_id: str,
        member_id: str,
        source_kind: str,
        source_ref: str,
        title: str,
        slug: str,
        markdown: str,
        status: str,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        draft_id = str(uuid.uuid4())
        created_at = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO adr_drafts
                   (id, generation_id, member_id, source_kind, source_ref, title, slug,
                    status, markdown, confidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    draft_id,
                    generation_id,
                    member_id,
                    source_kind,
                    source_ref,
                    title,
                    slug,
                    status,
                    markdown,
                    confidence,
                    created_at,
                ),
            )
        return self.get_draft(draft_id)  # type: ignore[return-value]

    @_synchronized
    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_drafts WHERE id=?", (draft_id,)
        ).fetchone()
        return _row(row)

    @_synchronized
    def get_draft_for_generation(self, generation_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_drafts WHERE generation_id=?", (generation_id,)
        ).fetchone()
        return _row(row)

    @_synchronized
    def list_drafts(self, member_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if member_id:
            rows = self._connection.execute(
                "SELECT * FROM adr_drafts WHERE member_id=? ORDER BY created_at DESC LIMIT ?",
                (member_id, max(1, min(int(limit), 500))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM adr_drafts ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def update_draft_status(self, draft_id: str, status: str) -> dict[str, Any] | None:
        with self._connection:
            self._connection.execute(
                "UPDATE adr_drafts SET status=? WHERE id=?", (status, draft_id)
            )
        return self.get_draft(draft_id)

    @_synchronized
    def mark_exported(self, draft_id: str, path: str) -> dict[str, Any] | None:
        with self._connection:
            self._connection.execute(
                "UPDATE adr_drafts SET exported_path=?, exported_at=? WHERE id=?",
                (path, _now(), draft_id),
            )
        return self.get_draft(draft_id)

    @_synchronized
    def delete_draft(self, draft_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute("DELETE FROM adr_drafts WHERE id=?", (draft_id,))
        return cursor.rowcount > 0


__all__ = ["AdrStore"]
