"""SQLite persistence for immutable endpoint explanation snapshots.

Rows may be populated while a candidate is pending/running/validating.  Once it
reaches a terminal state its graph and explanations are immutable.  Publishing
is a separate atomic operation which changes only the endpoint's current
pointer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from codeevolution.domain.explanation import (
    ExplanationChunk,
    ExplanationEdge,
    ExplanationNode,
    ExplanationSnapshot,
)

TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
PUBLISHABLE_STATUSES = frozenset({"completed", "partial"})
KNOWN_STATUSES = frozenset(
    {"pending", "running", "validating", "completed", "partial", "failed", "cancelled"}
)


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
    completed_at INTEGER
    ,repository_snapshot_id TEXT
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
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(explanation_snapshots)")}
        if "repository_snapshot_id" not in columns:
            self.connection.execute("ALTER TABLE explanation_snapshots ADD COLUMN repository_snapshot_id TEXT")
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
                    coverage, statistics, error, created_at, completed_at, repository_snapshot_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stored.id, stored.repo_name, stored.member_name, stored.api_key,
                    stored.method, stored.path, stored.handler, stored.entry_node_key,
                    stored.source_revision, stored.source_digest, stored.graph_digest,
                    stored.model_id, stored.prompt_version, stored.schema_version,
                    stored.status, _dump(stored.explanation), _dump(stored.coverage),
                    _dump(stored.statistics), stored.error, stored.created_at,
                    stored.completed_at, stored.repository_snapshot_id or None,
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
