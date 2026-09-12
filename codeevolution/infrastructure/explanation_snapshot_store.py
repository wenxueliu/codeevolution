"""SQLite persistence for immutable endpoint explanation snapshots.

Rows may be populated while a candidate is pending/running/validating.  Once it
reaches a terminal state its graph and explanations are immutable.  Publishing
is a separate atomic operation which changes only the endpoint's current
pointer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from codeevolution.domain.explanation import (
    ExplanationChunk,
    ExplanationEdge,
    ExplanationNode,
    ExplanationSnapshot,
)
from codeevolution.platform import ensure_supported_storage_path, set_private_permissions

TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
PUBLISHABLE_STATUSES = frozenset({"completed", "partial"})
KNOWN_STATUSES = frozenset(
    {"pending", "running", "validating", "completed", "partial", "failed", "cancelled"}
)
BATCH_STATUSES = frozenset({"queued", "running", "completed", "partial", "failed", "cancelled"})
BATCH_ITEM_STATUSES = frozenset({"queued", "running", "completed", "failed", "skipped", "cancelled"})


class SnapshotStateError(ValueError):
    """Raised when an operation violates the snapshot lifecycle."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS explanation_snapshots (
    id TEXT PRIMARY KEY,
    repo_name TEXT NOT NULL,
    member_name TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    handler TEXT NOT NULL,
    entry_node_key TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    graph_digest TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '{}',
    coverage TEXT NOT NULL DEFAULT '{}',
    statistics TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    repository_snapshot_id TEXT,
    prompt_profile_id TEXT NOT NULL DEFAULT '',
    prompt_digest TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_explanation_snapshots_endpoint
    ON explanation_snapshots(repo_name, member_name, api_key, created_at DESC);

CREATE TABLE IF NOT EXISTS explanation_snapshot_nodes (
    snapshot_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    local_digest TEXT NOT NULL,
    aggregate_digest TEXT NOT NULL,
    local_explanation TEXT NOT NULL,
    aggregate_explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, node_key),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS explanation_snapshot_chunks (
    snapshot_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, node_key, chunk_index),
    FOREIGN KEY (snapshot_id, node_key)
        REFERENCES explanation_snapshot_nodes(snapshot_id, node_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS explanation_snapshot_edges (
    snapshot_id TEXT NOT NULL,
    caller_key TEXT NOT NULL,
    callee_key TEXT NOT NULL,
    call_site TEXT NOT NULL DEFAULT '{}',
    call_context TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, caller_key, callee_key, call_site),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS current_explanation_snapshots (
    repo_name TEXT NOT NULL,
    member_name TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    PRIMARY KEY (repo_name, member_name, api_key),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_explanation_prompt_profiles (
    id TEXT PRIMARY KEY,
    repository_snapshot_id TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    prompt_templates TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL,
    prompt_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'web',
    is_current INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repository_snapshot_id, version),
    UNIQUE(repository_snapshot_id, prompt_digest)
);
CREATE INDEX IF NOT EXISTS idx_api_prompt_profiles_snapshot
    ON api_explanation_prompt_profiles(repository_snapshot_id, version DESC);

CREATE TABLE IF NOT EXISTS api_explanation_batch_jobs (
    id TEXT PRIMARY KEY,
    repository_snapshot_id TEXT NOT NULL,
    prompt_profile_id TEXT NOT NULL DEFAULT '',
    prompt_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    running_count INTEGER NOT NULL DEFAULT 0,
    cancelled_count INTEGER NOT NULL DEFAULT 0,
    concurrency INTEGER NOT NULL DEFAULT 2,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of_job_id TEXT,
    requested_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_api_batch_jobs_snapshot
    ON api_explanation_batch_jobs(repository_snapshot_id, requested_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_batch_active
    ON api_explanation_batch_jobs(repository_snapshot_id, prompt_digest)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS api_explanation_batch_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    endpoint_key TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    handler TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '',
    line INTEGER,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    explanation_snapshot_id TEXT,
    skip_reason TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at INTEGER,
    completed_at INTEGER,
    UNIQUE(batch_id, endpoint_key),
    FOREIGN KEY (batch_id) REFERENCES api_explanation_batch_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_api_batch_items_batch
    ON api_explanation_batch_items(batch_id, status, endpoint_key);
"""


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("explanation JSON fields must contain an object")
    return loaded


class ExplanationSnapshotStore:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        ensure_supported_storage_path(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(path.parent, directory=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        set_private_permissions(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(explanation_snapshots)")}
        if "repository_snapshot_id" not in columns:
            self.connection.execute("ALTER TABLE explanation_snapshots ADD COLUMN repository_snapshot_id TEXT")
        for column, definition in (
            ("prompt_profile_id", "TEXT NOT NULL DEFAULT ''"),
            ("prompt_digest", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE explanation_snapshots ADD COLUMN {column} {definition}"
                )
        prompt_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(api_explanation_prompt_profiles)")}
        if "prompt_templates" not in prompt_columns:
            self.connection.execute(
                "ALTER TABLE api_explanation_prompt_profiles ADD COLUMN prompt_templates TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.commit()
        self._lock = threading.RLock()

    def create_snapshot(self, snapshot: ExplanationSnapshot) -> ExplanationSnapshot:
        if snapshot.status not in KNOWN_STATUSES:
            raise ValueError(f"unknown snapshot status: {snapshot.status}")
        created_at = snapshot.created_at or int(time.time())
        stored = replace(snapshot, created_at=created_at)
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO explanation_snapshots
                   (id, repo_name, member_name, api_key, method, path, handler,
                    entry_node_key, source_revision, source_digest, graph_digest,
                    model_id, prompt_version, schema_version, status, explanation,
                   coverage, statistics, error, created_at, completed_at, repository_snapshot_id,
                   prompt_profile_id, prompt_digest)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stored.id, stored.repo_name, stored.member_name, stored.api_key,
                    stored.method, stored.path, stored.handler, stored.entry_node_key,
                    stored.source_revision, stored.source_digest, stored.graph_digest,
                    stored.model_id, stored.prompt_version, stored.schema_version,
                    stored.status, _dump(stored.explanation), _dump(stored.coverage),
                    _dump(stored.statistics), stored.error, stored.created_at,
                    stored.completed_at, stored.repository_snapshot_id or None,
                    stored.prompt_profile_id, stored.prompt_digest,
                ),
            )
        return stored

    def get_snapshot(self, snapshot_id: str) -> ExplanationSnapshot | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM explanation_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return self._snapshot(row) if row else None

    def list_snapshots(
        self, repo_name: str, member_name: str = "", api_key: str | None = None
    ) -> list[ExplanationSnapshot]:
        sql = "SELECT * FROM explanation_snapshots WHERE repo_name = ? AND member_name = ?"
        params: list[Any] = [repo_name, member_name]
        if api_key is not None:
            sql += " AND api_key = ?"
            params.append(api_key)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()
        return [self._snapshot(row) for row in rows]

    def list_snapshots_for_repository_snapshot(self, repository_snapshot_id: str, api_key: str | None = None) -> list[ExplanationSnapshot]:
        sql = "SELECT * FROM explanation_snapshots WHERE repository_snapshot_id = ?"
        params: list[Any] = [repository_snapshot_id]
        if api_key is not None:
            sql += " AND api_key = ?"
            params.append(api_key)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()
        return [self._snapshot(row) for row in rows]

    def get_current(
        self, repo_name: str, member_name: str, api_key: str
    ) -> ExplanationSnapshot | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT s.* FROM current_explanation_snapshots c
                   JOIN explanation_snapshots s ON s.id = c.snapshot_id
                   WHERE c.repo_name = ? AND c.member_name = ? AND c.api_key = ?""",
                (repo_name, member_name, api_key),
            ).fetchone()
        return self._snapshot(row) if row else None

    def get_current_for_repository_snapshot(self, repository_snapshot_id: str, api_key: str) -> ExplanationSnapshot | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT s.* FROM current_explanation_snapshots c
                   JOIN explanation_snapshots s ON s.id=c.snapshot_id
                   WHERE s.repository_snapshot_id=? AND s.api_key=?""",
                (repository_snapshot_id, api_key),
            ).fetchone()
        return self._snapshot(row) if row else None

    def update_status(
        self,
        snapshot_id: str,
        status: str,
        *,
        explanation: dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        statistics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ExplanationSnapshot:
        if status not in KNOWN_STATUSES:
            raise ValueError(f"unknown snapshot status: {status}")
        with self._lock, self.connection:
            current = self._require_snapshot(snapshot_id)
            if current.status in TERMINAL_STATUSES:
                raise SnapshotStateError(f"snapshot {snapshot_id} is immutable ({current.status})")
            completed_at = int(time.time()) if status in TERMINAL_STATUSES else None
            self.connection.execute(
                """UPDATE explanation_snapshots
                   SET status = ?, explanation = ?, coverage = ?, statistics = ?,
                       error = ?, completed_at = ? WHERE id = ?""",
                (
                    status,
                    _dump(current.explanation if explanation is None else explanation),
                    _dump(current.coverage if coverage is None else coverage),
                    _dump(current.statistics if statistics is None else statistics),
                    current.error if error is None else error,
                    completed_at,
                    snapshot_id,
                ),
            )
        return self.get_snapshot(snapshot_id)  # type: ignore[return-value]

    def update_snapshot(
        self,
        snapshot_id: str,
        status: str,
        *,
        explanation: dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        statistics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ExplanationSnapshot:
        """Compatibility name for advancing a candidate and saving its result."""
        return self.update_status(
            snapshot_id,
            status,
            explanation=explanation,
            coverage=coverage,
            statistics=statistics,
            error=error,
        )

    def is_cancelled(self, snapshot_id: str) -> bool:
        snapshot = self.get_snapshot(snapshot_id)
        return snapshot is not None and snapshot.status == "cancelled"

    def save_node(self, node: ExplanationNode) -> None:
        with self._lock, self.connection:
            self._require_mutable(node.snapshot_id)
            self.connection.execute(
                """INSERT INTO explanation_snapshot_nodes
                   (snapshot_id, node_key, source_hash, local_digest, aggregate_digest,
                    local_explanation, aggregate_explanation, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(snapshot_id, node_key) DO UPDATE SET
                     source_hash=excluded.source_hash, local_digest=excluded.local_digest,
                     aggregate_digest=excluded.aggregate_digest,
                     local_explanation=excluded.local_explanation,
                     aggregate_explanation=excluded.aggregate_explanation,
                     status=excluded.status""",
                (
                    node.snapshot_id, node.node_key, node.source_hash, node.local_digest,
                    node.aggregate_digest, _dump(node.local_explanation),
                    _dump(node.aggregate_explanation), node.status,
                ),
            )

    def get_node(self, snapshot_id: str, node_key: str) -> ExplanationNode | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM explanation_snapshot_nodes
                   WHERE snapshot_id = ? AND node_key = ?""",
                (snapshot_id, node_key),
            ).fetchone()
        return self._node(row) if row else None

    def list_nodes(self, snapshot_id: str) -> list[ExplanationNode]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM explanation_snapshot_nodes WHERE snapshot_id = ? ORDER BY node_key",
                (snapshot_id,),
            ).fetchall()
        return [self._node(row) for row in rows]

    def save_chunk(self, chunk: ExplanationChunk) -> None:
        if chunk.line_start < 1 or chunk.line_end < chunk.line_start:
            raise ValueError("chunk must have a valid inclusive line range")
        with self._lock, self.connection:
            self._require_mutable(chunk.snapshot_id)
            self.connection.execute(
                """INSERT INTO explanation_snapshot_chunks
                   (snapshot_id, node_key, chunk_index, line_start, line_end,
                    source_hash, explanation, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(snapshot_id, node_key, chunk_index) DO UPDATE SET
                     line_start=excluded.line_start, line_end=excluded.line_end,
                     source_hash=excluded.source_hash, explanation=excluded.explanation,
                     status=excluded.status""",
                (
                    chunk.snapshot_id, chunk.node_key, chunk.chunk_index,
                    chunk.line_start, chunk.line_end, chunk.source_hash,
                    _dump(chunk.explanation), chunk.status,
                ),
            )

    def list_chunks(self, snapshot_id: str, node_key: str) -> list[ExplanationChunk]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM explanation_snapshot_chunks
                   WHERE snapshot_id = ? AND node_key = ? ORDER BY chunk_index""",
                (snapshot_id, node_key),
            ).fetchall()
        return [self._chunk(row) for row in rows]

    def save_edge(self, edge: ExplanationEdge) -> None:
        with self._lock, self.connection:
            self._require_mutable(edge.snapshot_id)
            self.connection.execute(
                """INSERT OR REPLACE INTO explanation_snapshot_edges
                   (snapshot_id, caller_key, callee_key, call_site, call_context)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    edge.snapshot_id, edge.caller_key, edge.callee_key,
                    _dump(edge.call_site), _dump(edge.call_context),
                ),
            )

    def list_edges(self, snapshot_id: str) -> list[ExplanationEdge]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM explanation_snapshot_edges WHERE snapshot_id = ?
                   ORDER BY caller_key, callee_key, call_site""",
                (snapshot_id,),
            ).fetchall()
        return [self._edge(row) for row in rows]

    def publish(self, snapshot_id: str) -> ExplanationSnapshot:
        """Atomically make a completed/partial snapshot current for its endpoint."""
        with self._lock, self.connection:
            snapshot = self._require_snapshot(snapshot_id)
            if snapshot.status not in PUBLISHABLE_STATUSES:
                raise SnapshotStateError(
                    f"snapshot {snapshot_id} cannot be published with status {snapshot.status}"
                )
            self.connection.execute(
                """INSERT INTO current_explanation_snapshots
                   (repo_name, member_name, api_key, snapshot_id) VALUES (?, ?, ?, ?)
                   ON CONFLICT(repo_name, member_name, api_key)
                   DO UPDATE SET snapshot_id = excluded.snapshot_id""",
                (snapshot.repo_name, snapshot.member_name, snapshot.api_key, snapshot.id),
            )
        return snapshot

    def cancel(self, snapshot_id: str) -> ExplanationSnapshot:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        if snapshot.status not in {"pending", "running", "validating"}:
            raise SnapshotStateError(f"snapshot {snapshot_id} cannot be cancelled ({snapshot.status})")
        return self.update_status(snapshot_id, "cancelled")

    def delete(self, snapshot_id: str, *, confirm_current: bool = False) -> None:
        with self._lock, self.connection:
            snapshot = self._require_snapshot(snapshot_id)
            if snapshot.status == "running":
                raise SnapshotStateError("running snapshot must be cancelled before deletion")
            current = self.connection.execute(
                "SELECT 1 FROM current_explanation_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if current and not confirm_current:
                raise SnapshotStateError("deleting the current snapshot requires explicit confirmation")
            if current:
                self.connection.execute(
                    "DELETE FROM current_explanation_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            self.connection.execute("DELETE FROM explanation_snapshots WHERE id = ?", (snapshot_id,))

    # Prompt profiles -------------------------------------------------

    def create_prompt_profile(
        self, repository_snapshot_id: str, prompt_text: str = "", *,
        prompt_templates: dict[str, Any] | None = None, created_by: str = "web"
    ) -> dict[str, Any]:
        text = " ".join(str(prompt_text or "").split())
        templates = {
            key: str(value)
            for key, value in (prompt_templates or {}).items()
            if key in {"local", "synthesis", "aggregate"} and str(value).strip()
        }
        if not text and not templates:
            raise ValueError("prompt_text or prompt_templates is required")
        digest_payload = json.dumps(
            {"prompt_text": text, "prompt_templates": templates},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        templates_json = _dump(templates)
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM api_explanation_prompt_profiles WHERE repository_snapshot_id=? AND prompt_digest=?",
                (repository_snapshot_id, digest),
            ).fetchone()
            self.connection.execute(
                "UPDATE api_explanation_prompt_profiles SET is_current=0 WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            )
            if existing:
                self.connection.execute(
                    "UPDATE api_explanation_prompt_profiles SET is_current=1 WHERE id=?",
                    (existing["id"],),
                )
                row = self.connection.execute(
                    "SELECT * FROM api_explanation_prompt_profiles WHERE id=?", (existing["id"],)
                ).fetchone()
                return self._prompt_profile(row)
            version = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM api_explanation_prompt_profiles WHERE repository_snapshot_id=?",
                (repository_snapshot_id,),
            ).fetchone()["next_version"]
            profile_id = f"prompt-{repository_snapshot_id[:8]}-v{version}-{uuid.uuid4().hex[:8]}"
            self.connection.execute(
                """INSERT INTO api_explanation_prompt_profiles
                   (id, repository_snapshot_id, prompt_text, prompt_templates, version, prompt_digest, created_at, created_by, is_current)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (profile_id, repository_snapshot_id, text, templates_json, version, digest, int(time.time()), created_by),
            )
            row = self.connection.execute(
                "SELECT * FROM api_explanation_prompt_profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return self._prompt_profile(row)

    def list_prompt_profiles(self, repository_snapshot_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM api_explanation_prompt_profiles WHERE repository_snapshot_id=? ORDER BY version DESC",
                (repository_snapshot_id,),
            ).fetchall()
        return [self._prompt_profile(row) for row in rows]

    def get_prompt_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM api_explanation_prompt_profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return self._prompt_profile(row) if row else None

    def get_current_prompt_profile(self, repository_snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM api_explanation_prompt_profiles WHERE repository_snapshot_id=? AND is_current=1",
                (repository_snapshot_id,),
            ).fetchone()
        return self._prompt_profile(row) if row else None

    # Batch jobs ------------------------------------------------------

    def get_active_batch(self, repository_snapshot_id: str, prompt_digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM api_explanation_batch_jobs
                   WHERE repository_snapshot_id=? AND prompt_digest=? AND status IN ('queued','running')
                   ORDER BY requested_at DESC LIMIT 1""",
                (repository_snapshot_id, prompt_digest),
            ).fetchone()
        return self._batch(row, include_items=False) if row else None

    def create_batch(
        self, repository_snapshot_id: str, prompt_profile_id: str, prompt_digest: str,
        items: list[dict[str, Any]], *, concurrency: int = 2, retry_of_job_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= int(concurrency) <= 5:
            raise ValueError("concurrency must be between 1 and 5")
        job_id = f"batch-{uuid.uuid4().hex}"
        now = int(time.time())
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = str(item.get("endpoint_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO api_explanation_batch_jobs
                   (id, repository_snapshot_id, prompt_profile_id, prompt_digest, status,
                    total_count, concurrency, retry_of_job_id, requested_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (job_id, repository_snapshot_id, prompt_profile_id, prompt_digest,
                 len(normalized), int(concurrency), retry_of_job_id, now),
            )
            for item in normalized:
                key = str(item.get("endpoint_key") or "")
                status = str(item.get("status") or "queued")
                if status not in BATCH_ITEM_STATUSES:
                    raise ValueError(f"unknown batch item status: {status}")
                self.connection.execute(
                    """INSERT INTO api_explanation_batch_items
                       (id, batch_id, endpoint_key, method, path, handler, file, line, status,
                        explanation_snapshot_id, skip_reason, error_message, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"item-{uuid.uuid4().hex}", job_id, key, str(item.get("method") or ""),
                     str(item.get("path") or ""), str(item.get("handler") or ""),
                     str(item.get("file") or ""), item.get("line"), status,
                     item.get("explanation_snapshot_id"), str(item.get("skip_reason") or ""),
                     str(item.get("error_message") or ""), now if status in {"skipped", "cancelled"} else None),
                )
            self._refresh_batch_locked(job_id)
        return self.get_batch(job_id)  # type: ignore[return-value]

    def list_batches(self, repository_snapshot_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM api_explanation_batch_jobs WHERE repository_snapshot_id=? ORDER BY requested_at DESC LIMIT ?",
                (repository_snapshot_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._batch(row, include_items=False) for row in rows]

    def get_batch(self, batch_id: str, *, include_items: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            items = self.connection.execute(
                "SELECT * FROM api_explanation_batch_items WHERE batch_id=? ORDER BY endpoint_key",
                (batch_id,),
            ).fetchall() if include_items else []
        return self._batch(row, items=items, include_items=include_items)

    def claim_batch_items(self, batch_id: str, limit: int) -> list[dict[str, Any]]:
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT * FROM api_explanation_batch_items WHERE batch_id=? AND status='queued' ORDER BY endpoint_key LIMIT ?",
                (batch_id, max(0, int(limit))),
            ).fetchall()
            claimed = []
            for row in rows:
                now = int(time.time())
                self.connection.execute(
                    "UPDATE api_explanation_batch_items SET status='running',attempt_count=attempt_count+1,started_at=?,error_message='' WHERE id=? AND status='queued'",
                    (now, row["id"]),
                )
                claimed.append(dict(row) | {"status": "running", "attempt_count": row["attempt_count"] + 1, "started_at": now})
            self._refresh_batch_locked(batch_id, force_running=True)
        return claimed

    def start_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row["status"] not in {"queued", "running"}:
                return self._batch(row)
            if row["status"] == "queued":
                self.connection.execute(
                    "UPDATE api_explanation_batch_jobs SET status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                    (int(time.time()), batch_id),
                )
            self._refresh_batch_locked(batch_id, force_running=True)
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
        return self._batch(row)

    def update_batch_item(self, item_id: str, status: str, *, explanation_snapshot_id: str | None = None,
                          error_message: str | None = None, skip_reason: str | None = None) -> dict[str, Any]:
        if status not in BATCH_ITEM_STATUSES:
            raise ValueError(f"unknown batch item status: {status}")
        with self._lock, self.connection:
            row = self.connection.execute("SELECT * FROM api_explanation_batch_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            completed_at = int(time.time()) if status in {"completed", "failed", "skipped", "cancelled"} else None
            self.connection.execute(
                """UPDATE api_explanation_batch_items SET status=?, explanation_snapshot_id=COALESCE(?, explanation_snapshot_id),
                   error_message=COALESCE(?, error_message), skip_reason=COALESCE(?, skip_reason), completed_at=? WHERE id=?""",
                (status, explanation_snapshot_id, error_message, skip_reason, completed_at, item_id),
            )
            self._refresh_batch_locked(row["batch_id"])
            updated = self.connection.execute("SELECT * FROM api_explanation_batch_items WHERE id=?", (item_id,)).fetchone()
        return self._batch_item(updated)

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row["status"] in {"completed", "partial", "failed", "cancelled"}:
                return self._batch(row)
            now = int(time.time())
            self.connection.execute("UPDATE api_explanation_batch_jobs SET cancel_requested=1 WHERE id=?", (batch_id,))
            self.connection.execute(
                "UPDATE api_explanation_batch_items SET status='cancelled',completed_at=? WHERE batch_id=? AND status='queued'",
                (now, batch_id),
            )
            self._refresh_batch_locked(batch_id)
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
        return self._batch(row)

    def fail_batch(self, batch_id: str, error_message: str) -> dict[str, Any]:
        with self._lock, self.connection:
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            self.connection.execute(
                "UPDATE api_explanation_batch_jobs SET status='failed',error_message=?,completed_at=? WHERE id=?",
                (error_message, int(time.time()), batch_id),
            )
            row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
        return self._batch(row)

    def recover_interrupted_batches(self) -> list[str]:
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT id FROM api_explanation_batch_jobs WHERE status IN ('queued','running')"
            ).fetchall()
            self.connection.execute(
                "UPDATE api_explanation_batch_items SET status='queued',started_at=NULL WHERE status='running'"
            )
            self.connection.execute(
                "UPDATE api_explanation_batch_jobs SET status='queued',started_at=NULL,running_count=0 WHERE status='running'"
            )
        return [row["id"] for row in rows]

    def _refresh_batch_locked(self, batch_id: str, *, force_running: bool = False) -> None:
        row = self.connection.execute("SELECT * FROM api_explanation_batch_jobs WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            return
        counts = {
            status: self.connection.execute(
                "SELECT COUNT(*) AS count FROM api_explanation_batch_items WHERE batch_id=? AND status=?",
                (batch_id, status),
            ).fetchone()["count"]
            for status in BATCH_ITEM_STATUSES
        }
        status = row["status"]
        terminal = counts["queued"] == 0 and counts["running"] == 0
        if terminal:
            if row["cancel_requested"]:
                status = "partial" if counts["completed"] else "cancelled"
            elif counts["failed"] and not counts["completed"]:
                status = "failed"
            elif counts["failed"] or counts["skipped"] or counts["cancelled"]:
                status = "partial"
            else:
                status = "completed"
        elif force_running or status == "running":
            status = "running"
        started_at = row["started_at"] or (int(time.time()) if status == "running" else None)
        completed_at = int(time.time()) if status in BATCH_STATUSES - {"queued", "running"} else None
        self.connection.execute(
            """UPDATE api_explanation_batch_jobs SET status=?,total_count=?,completed_count=?,failed_count=?,
               skipped_count=?,running_count=?,cancelled_count=?,started_at=?,completed_at=? WHERE id=?""",
            (status, sum(counts.values()), counts["completed"], counts["failed"], counts["skipped"],
             counts["running"], counts["cancelled"], started_at, completed_at, batch_id),
        )

    @staticmethod
    def _prompt_profile(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "repository_snapshot_id": row["repository_snapshot_id"],
                "prompt_text": row["prompt_text"],
                "prompt_templates": _load(row["prompt_templates"] or "{}"),
                "version": row["version"],
                "prompt_digest": row["prompt_digest"], "created_at": row["created_at"],
                "created_by": row["created_by"], "is_current": bool(row["is_current"])}

    @staticmethod
    def _batch_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return item

    def _batch(self, row: sqlite3.Row, *, items=None, include_items: bool = True) -> dict[str, Any]:
        result = dict(row)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        result["progress"] = {
            "total": result.get("total_count", 0), "completed": result.get("completed_count", 0),
            "failed": result.get("failed_count", 0), "skipped": result.get("skipped_count", 0),
            "running": result.get("running_count", 0), "cancelled": result.get("cancelled_count", 0),
        }
        total = result["progress"]["total"]
        done = sum(result["progress"][key] for key in ("completed", "failed", "skipped", "cancelled"))
        result["progress"]["percent"] = round(done / total * 100) if total else 100
        if include_items:
            result["items"] = [self._batch_item(item) for item in (items if items is not None else [])]
        return result

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _require_snapshot(self, snapshot_id: str) -> ExplanationSnapshot:
        row = self.connection.execute(
            "SELECT * FROM explanation_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return self._snapshot(row)

    def _require_mutable(self, snapshot_id: str) -> None:
        snapshot = self._require_snapshot(snapshot_id)
        if snapshot.status in TERMINAL_STATUSES:
            raise SnapshotStateError(f"snapshot {snapshot_id} is immutable ({snapshot.status})")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ExplanationSnapshot:
        return ExplanationSnapshot(
            id=row["id"], repo_name=row["repo_name"], member_name=row["member_name"],
            api_key=row["api_key"], method=row["method"], path=row["path"],
            handler=row["handler"], entry_node_key=row["entry_node_key"],
            source_revision=row["source_revision"], source_digest=row["source_digest"],
            graph_digest=row["graph_digest"], model_id=row["model_id"],
            prompt_version=row["prompt_version"], schema_version=row["schema_version"],
            status=row["status"], explanation=_load(row["explanation"]),
            coverage=_load(row["coverage"]), statistics=_load(row["statistics"]),
            error=row["error"], created_at=row["created_at"], completed_at=row["completed_at"],
            repository_snapshot_id=row["repository_snapshot_id"] or "",
            prompt_profile_id=row["prompt_profile_id"] or "",
            prompt_digest=row["prompt_digest"] or "",
        )

    @staticmethod
    def _node(row: sqlite3.Row) -> ExplanationNode:
        return ExplanationNode(
            snapshot_id=row["snapshot_id"], node_key=row["node_key"],
            source_hash=row["source_hash"], local_digest=row["local_digest"],
            aggregate_digest=row["aggregate_digest"],
            local_explanation=_load(row["local_explanation"]),
            aggregate_explanation=_load(row["aggregate_explanation"]), status=row["status"],
        )

    @staticmethod
    def _chunk(row: sqlite3.Row) -> ExplanationChunk:
        return ExplanationChunk(
            snapshot_id=row["snapshot_id"], node_key=row["node_key"],
            chunk_index=row["chunk_index"], line_start=row["line_start"],
            line_end=row["line_end"], source_hash=row["source_hash"],
            explanation=_load(row["explanation"]), status=row["status"],
        )

    @staticmethod
    def _edge(row: sqlite3.Row) -> ExplanationEdge:
        return ExplanationEdge(
            snapshot_id=row["snapshot_id"], caller_key=row["caller_key"],
            callee_key=row["callee_key"], call_site=_load(row["call_site"]),
            call_context=_load(row["call_context"]),
        )
