"""ADR generation storage: per-service prompts, generation traces and drafts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import environment_value

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

CREATE TABLE IF NOT EXISTS adr_draft_revisions (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES adr_drafts(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    markdown TEXT NOT NULL,
    editor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(draft_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_adr_draft_revisions_draft
    ON adr_draft_revisions(draft_id, revision DESC);

CREATE TABLE IF NOT EXISTS adr_templates (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    template_text TEXT NOT NULL,
    template_digest TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adr_templates_member
    ON adr_templates(member_id, version);

CREATE TABLE IF NOT EXISTS adr_commit_scans (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    committed_at TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    rule_candidate INTEGER NOT NULL DEFAULT 1,
    rule_reason TEXT NOT NULL DEFAULT '',
    ai_candidate INTEGER,
    ai_reason TEXT NOT NULL DEFAULT '',
    change_type TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_digest TEXT NOT NULL DEFAULT '',
    scanned_at TEXT NOT NULL DEFAULT '',
    UNIQUE(member_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_adr_commit_scans_member
    ON adr_commit_scans(member_id, scanned_at);

-- A human can overrule an automatic skip (merge/noise/model decision) and put
-- the commit back in the candidate queue, or restore the skip afterwards.
CREATE TABLE IF NOT EXISTS adr_commit_overrides (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'candidate',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(member_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_adr_commit_overrides_member
    ON adr_commit_overrides(member_id);

CREATE TABLE IF NOT EXISTS adr_backtests (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT '',
    confidence REAL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    prompt_rendered TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


# Listing endpoints page through rows. The largest accepted page is
# configurable so a large installation can page through more rows at once
# without a silent clip: set ``CODEEVOLUTION_ADR_LIST_LIMIT``.
DEFAULT_MAX_LIST_LIMIT = 5000


def max_list_limit() -> int:
    """Largest page a listing accepts (env ``CODEEVOLUTION_ADR_LIST_LIMIT``)."""
    raw = environment_value(
        "CODEEVOLUTION_ADR_LIST_LIMIT", str(DEFAULT_MAX_LIST_LIMIT)
    ).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_LIST_LIMIT


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
        draft_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(adr_drafts)")
        }
        additions = {
            "review_status": "TEXT NOT NULL DEFAULT 'pending'",
            "rating": "INTEGER",
            "review_comment": "TEXT NOT NULL DEFAULT ''",
            "reviewed_at": "TEXT",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "editor": "TEXT NOT NULL DEFAULT ''",
            "reviewer": "TEXT NOT NULL DEFAULT ''",
            "edited_at": "TEXT",
        }
        for column, definition in additions.items():
            if column not in draft_columns:
                with self._connection:
                    self._connection.execute(
                        f"ALTER TABLE adr_drafts ADD COLUMN {column} {definition}"
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
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Newest-first generations, paged with ``offset`` up to the page cap.

        The limit is honoured up to ``max_list_limit()`` (5000 by default,
        configurable) instead of being clipped to a smaller hidden value, and
        ``offset`` keeps older rows reachable beyond one page.
        """
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
            f"SELECT * FROM adr_generations{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), max_list_limit())), max(0, int(offset))),
        ).fetchall()
        return [decoded for row in rows if (decoded := self._decode_generation(row))]

    @_synchronized
    def generation_counts_by_kind(self, member_id: str) -> dict[str, int]:
        """Generation totals per source kind, straight from SQL (no page cap)."""
        rows = self._connection.execute(
            "SELECT source_kind, COUNT(*) AS count FROM adr_generations"
            " WHERE member_id=? GROUP BY source_kind",
            (member_id,),
        ).fetchall()
        return {row["source_kind"]: row["count"] for row in rows}

    @_synchronized
    def latest_generations_by_kind(self, member_id: str) -> list[dict[str, Any]]:
        """Newest generation per source kind, used by the source mapping panel."""
        return self._latest_by(
            "adr_generations", member_id, "source_kind", self._decode_generation
        )

    @_synchronized
    def latest_drafts_by_kind(self, member_id: str) -> list[dict[str, Any]]:
        """Newest draft per source kind, used by the source mapping panel."""
        return self._latest_by("adr_drafts", member_id, "source_kind", lambda row: dict(row))

    def _latest_by(self, table: str, member_id: str, partition: str, decode):
        rows = self._connection.execute(
            f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY {partition} ORDER BY created_at DESC, rowid DESC
                ) AS _row_number
                FROM {table}
                WHERE member_id=?
            ) WHERE _row_number=1
            """,
            (member_id,),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = decode(row)
            if item is None:
                continue
            item.pop("_row_number", None)
            items.append(item)
        return items

    @_synchronized
    def latest_commit_generations(self, member_id: str) -> list[dict[str, Any]]:
        """One newest generation per commit sha, without a page cap.

        The commit table maps every row to its latest generation.  Reusing the
        paged ``list_generations`` capped the mapping at the newest N rows, so
        commits generated earlier silently fell back to "pending" — and the
        progress counts under-reported them.  This returns exactly one row per
        source, so the mapping is complete at any history size.
        """
        rows = self._connection.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY source_ref ORDER BY created_at DESC, rowid DESC
                ) AS _row_number
                FROM adr_generations
                WHERE member_id=? AND source_kind='commit'
            ) WHERE _row_number=1
            """,
            (member_id,),
        ).fetchall()
        generations: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode_generation(row)
            if item is None:
                continue
            item.pop("_row_number", None)
            generations.append(item)
        return generations

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
            self._connection.execute(
                """INSERT INTO adr_draft_revisions
                   (id, draft_id, revision, title, markdown, editor, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), draft_id, 1, title, markdown, "", "生成草稿", created_at),
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
                (member_id, max(1, min(int(limit), max_list_limit()))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM adr_drafts ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), max_list_limit())),),
            ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def commit_drafts(self, member_id: str) -> list[dict[str, Any]]:
        """One newest commit-sourced draft per source sha, without a page cap."""
        rows = self._connection.execute(
            """
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY source_ref ORDER BY created_at DESC, rowid DESC
                ) AS _row_number
                FROM adr_drafts
                WHERE member_id=? AND source_kind='commit'
            ) WHERE _row_number=1
            """,
            (member_id,),
        ).fetchall()
        return [
            {key: value for key, value in dict(row).items() if key != "_row_number"}
            for row in rows
        ]

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

    @_synchronized
    def add_revision(
        self,
        draft_id: str,
        *,
        revision: int,
        markdown: str,
        title: str = "",
        editor: str = "",
        note: str = "",
    ) -> dict[str, Any] | None:
        revision_id = str(uuid.uuid4())
        created_at = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO adr_draft_revisions
                   (id, draft_id, revision, title, markdown, editor, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (revision_id, draft_id, int(revision), title, markdown, editor, note, created_at),
            )
        row = self._connection.execute(
            "SELECT * FROM adr_draft_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def list_revisions(self, draft_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT * FROM adr_draft_revisions WHERE draft_id=?
               ORDER BY revision DESC LIMIT ?""",
            (draft_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def delete_backtest_for_draft(self, draft_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM adr_backtests WHERE draft_id=?", (draft_id,)
            )
        return cursor.rowcount > 0


    # ── output templates ─────────────────────────────────────────────

    @_synchronized
    def create_template(self, member_id: str, template_text: str, digest: str) -> dict[str, Any]:
        current = self.current_template(member_id)
        version = (current["version"] + 1) if current else 1
        template_id = str(uuid.uuid4())
        created_at = _now()
        with self._connection:
            self._connection.execute(
                """INSERT INTO adr_templates
                   (id, member_id, template_text, template_digest, version, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (template_id, member_id, template_text, digest, version, created_at),
            )
        return {
            "id": template_id,
            "member_id": member_id,
            "template_text": template_text,
            "template_digest": digest,
            "version": version,
            "created_at": created_at,
        }

    @_synchronized
    def current_template(self, member_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_templates WHERE member_id=? ORDER BY version DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        return _row(row)

    @_synchronized
    def delete_templates(self, member_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM adr_templates WHERE member_id=?", (member_id,)
            )
        return cursor.rowcount > 0

    # ── commit triage cache ──────────────────────────────────────────

    @_synchronized
    def upsert_scan(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self._connection.execute(
            "SELECT id FROM adr_commit_scans WHERE member_id=? AND sha=?",
            (record["member_id"], record["sha"]),
        ).fetchone()
        payload = (
            record["member_id"],
            record["sha"],
            record.get("subject", ""),
            record.get("author", ""),
            record.get("committed_at", ""),
            json.dumps(record.get("files") or [], ensure_ascii=False),
            1 if record.get("rule_candidate", True) else 0,
            record.get("rule_reason", ""),
            None if record.get("ai_candidate") is None else (1 if record["ai_candidate"] else 0),
            record.get("ai_reason", ""),
            record.get("change_type", ""),
            record.get("model", ""),
            record.get("prompt_digest", ""),
            record.get("scanned_at") or _now(),
        )
        with self._connection:
            if existing:
                self._connection.execute(
                    """UPDATE adr_commit_scans SET
                       subject=?, author=?, committed_at=?, files_json=?, rule_candidate=?,
                       rule_reason=?, ai_candidate=?, ai_reason=?, change_type=?, model=?,
                       prompt_digest=?, scanned_at=?
                       WHERE member_id=? AND sha=?""",
                    (*payload[2:], payload[0], payload[1]),
                )
            else:
                self._connection.execute(
                    """INSERT INTO adr_commit_scans
                       (id, member_id, sha, subject, author, committed_at, files_json,
                        rule_candidate, rule_reason, ai_candidate, ai_reason, change_type,
                        model, prompt_digest, scanned_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), *payload),
                )
        return self.get_scan(record["member_id"], record["sha"])  # type: ignore[return-value]

    @_synchronized
    def get_scan(self, member_id: str, sha: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_commit_scans WHERE member_id=? AND sha=?", (member_id, sha)
        ).fetchone()
        return self._decode_scan(row)

    @_synchronized
    def list_scans(self, member_id: str, shas: list[str] | None = None) -> list[dict[str, Any]]:
        if shas:
            placeholders = ",".join("?" for _ in shas)
            rows = self._connection.execute(
                f"SELECT * FROM adr_commit_scans WHERE member_id=? AND sha IN ({placeholders})",
                (member_id, *shas),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM adr_commit_scans WHERE member_id=? ORDER BY committed_at DESC",
                (member_id,),
            ).fetchall()
        return [decoded for row in rows if (decoded := self._decode_scan(row))]

    @staticmethod
    def _decode_scan(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["files"] = _loads(data.pop("files_json", "[]"), [])
        data["rule_candidate"] = bool(data.get("rule_candidate"))
        if data.get("ai_candidate") is not None:
            data["ai_candidate"] = bool(data["ai_candidate"])
        return data

    # ── human overrides of automatic skips ──────────────────────────

    @_synchronized
    def get_commit_override(self, member_id: str, sha: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_commit_overrides WHERE member_id=? AND sha=?",
            (member_id, sha),
        ).fetchone()
        return dict(row) if row is not None else None

    @_synchronized
    def list_commit_overrides(self, member_id: str) -> dict[str, dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM adr_commit_overrides WHERE member_id=?", (member_id,)
        ).fetchall()
        return {row["sha"]: dict(row) for row in rows}

    @_synchronized
    def upsert_commit_override(
        self,
        *,
        member_id: str,
        sha: str,
        action: str = "candidate",
        reason: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        now = _now()
        existing = self.get_commit_override(member_id, sha)
        with self._connection:
            if existing is None:
                self._connection.execute(
                    """INSERT INTO adr_commit_overrides
                       (id, member_id, sha, action, reason, actor, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), member_id, sha, action, reason, actor, now, now),
                )
            else:
                self._connection.execute(
                    """UPDATE adr_commit_overrides
                       SET action=?, reason=?, actor=?, updated_at=?
                       WHERE member_id=? AND sha=?""",
                    (action, reason, actor, now, member_id, sha),
                )
        return self.get_commit_override(member_id, sha)  # type: ignore[return-value]

    @_synchronized
    def delete_commit_override(self, member_id: str, sha: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM adr_commit_overrides WHERE member_id=? AND sha=?",
                (member_id, sha),
            )
        return cursor.rowcount > 0

    # ── backtests ────────────────────────────────────────────────────

    @_synchronized
    def upsert_backtest(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self._connection.execute(
            "SELECT id FROM adr_backtests WHERE draft_id=?", (record["draft_id"],)
        ).fetchone()
        backtest_id = existing["id"] if existing else str(uuid.uuid4())
        payload = (
            record["draft_id"],
            record.get("status", "ready"),
            record.get("verdict", ""),
            record.get("confidence"),
            json.dumps(record.get("reasons") or [], ensure_ascii=False),
            json.dumps(record.get("evidence") or [], ensure_ascii=False),
            record.get("recommendation", ""),
            record.get("model", ""),
            record.get("prompt_text", ""),
            record.get("prompt_rendered", ""),
            record.get("raw_response", ""),
            record.get("error_message", ""),
            int(record.get("duration_ms") or 0),
            record.get("created_at") or _now(),
            record.get("completed_at") or _now(),
        )
        with self._connection:
            if existing:
                self._connection.execute(
                    """UPDATE adr_backtests SET status=?, verdict=?, confidence=?, reasons_json=?,
                       evidence_json=?, recommendation=?, model=?, prompt_text=?, prompt_rendered=?,
                       raw_response=?, error_message=?, duration_ms=?, created_at=?, completed_at=?
                       WHERE draft_id=?""",
                    (*payload[1:], payload[0]),
                )
            else:
                self._connection.execute(
                    """INSERT INTO adr_backtests
                       (id, draft_id, status, verdict, confidence, reasons_json, evidence_json,
                        recommendation, model, prompt_text, prompt_rendered, raw_response,
                        error_message, duration_ms, created_at, completed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (backtest_id, *payload),
                )
        return self.get_backtest(record["draft_id"])  # type: ignore[return-value]

    @_synchronized
    def get_backtest(self, draft_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM adr_backtests WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["reasons"] = _loads(data.pop("reasons_json", "[]"), [])
        data["evidence"] = _loads(data.pop("evidence_json", "[]"), [])
        return data

    @_synchronized
    def backtests_for(self, draft_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Backtests keyed by draft id, in chunks so large tables stay one query each."""
        results: dict[str, dict[str, Any]] = {}
        unique = list(dict.fromkeys(draft_id for draft_id in draft_ids if draft_id))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT * FROM adr_backtests WHERE draft_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                data = dict(row)
                data["reasons"] = _loads(data.pop("reasons_json", "[]"), [])
                data["evidence"] = _loads(data.pop("evidence_json", "[]"), [])
                results[data["draft_id"]] = data
        return results

    # ── draft review ─────────────────────────────────────────────────

    @_synchronized
    def update_draft(self, draft_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "markdown",
            "review_status",
            "rating",
            "review_comment",
            "reviewed_at",
            "exported_path",
            "exported_at",
            "revision",
            "editor",
            "reviewer",
            "edited_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_draft(draft_id)
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connection:
            self._connection.execute(
                f"UPDATE adr_drafts SET {assignments} WHERE id=?",
                (*updates.values(), draft_id),
            )
        return self.get_draft(draft_id)


__all__ = ["AdrStore"]
