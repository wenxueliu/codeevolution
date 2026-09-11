"""Persistent store for LLM-generated per-call-chain-node business rule explanations.

A rule is keyed by the node it describes:

  * ``func``  — an intra-repo function node; ``node_key`` = ``<member>::<qualified_name>``
  * ``cross`` — a matched downstream service handler; ``node_key`` = ``<service>::<handler>``

Regenerating a node's rule overwrites the stored row (upsert on the unique key),
so previously saved explanations are updated in place.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from ..platform import ensure_supported_storage_path, set_private_permissions

SCHEMA = """CREATE TABLE IF NOT EXISTS node_business_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    member TEXT NOT NULL DEFAULT '',
    node_type TEXT NOT NULL,
    node_key TEXT NOT NULL,
    custom_prompt TEXT NOT NULL DEFAULT '',
    result TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    error TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(repo_name, member, node_type, node_key)
)"""


class NodeRuleStore:
    def __init__(self, db_path: str):
        path = Path(db_path)
        ensure_supported_storage_path(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(path.parent, directory=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        set_private_permissions(path)
        self._lock = threading.Lock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(SCHEMA)
        self.connection.commit()

    # ── query ──

    def get(
        self, repo_name: str, member: str, node_type: str, node_key: str
    ) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM node_business_rules
                   WHERE repo_name = ? AND member = ? AND node_type = ? AND node_key = ?""",
                (repo_name, member, node_type, node_key),
            ).fetchone()
        return dict(row) if row else None

    # ── upsert ──

    def upsert(
        self,
        *,
        repo_name: str,
        member: str,
        node_type: str,
        node_key: str,
        custom_prompt: str = "",
        result: str = "",
        status: str = "idle",
        error: str = "",
    ) -> int:
        now = int(time.time())
        with self._lock:
            existing = self.connection.execute(
                """SELECT id FROM node_business_rules
                   WHERE repo_name = ? AND member = ? AND node_type = ? AND node_key = ?""",
                (repo_name, member, node_type, node_key),
            ).fetchone()
            if existing:
                self.connection.execute(
                    """UPDATE node_business_rules
                       SET custom_prompt = ?, result = ?, status = ?, error = ?, updated_at = ?
                       WHERE id = ?""",
                    (custom_prompt, result, status, error, now, existing["id"]),
                )
                self.connection.commit()
                return existing["id"]
            cursor = self.connection.execute(
                """INSERT INTO node_business_rules
                   (repo_name, member, node_type, node_key, custom_prompt, result, status, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (repo_name, member, node_type, node_key, custom_prompt, result, status, error, now, now),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def update_status(self, rule_id: int, *, status: str, result: str = "", error: str = "") -> None:
        now = int(time.time())
        with self._lock:
            self.connection.execute(
                "UPDATE node_business_rules SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, result, error, now, rule_id),
            )
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()
