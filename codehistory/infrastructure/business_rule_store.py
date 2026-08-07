"""Persistent store for LLM-generated API business rules."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


SCHEMA = """CREATE TABLE IF NOT EXISTS api_business_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name TEXT NOT NULL,
    handler TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    custom_prompt TEXT NOT NULL DEFAULT '',
    result TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    error TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(repo_name, handler, method, path)
)"""


class BusinessRuleStore:
    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(SCHEMA)
        self.connection.commit()

    # ── query ──

    def get(
        self, repo_name: str, handler: str, method: str, path: str
    ) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM api_business_rules
                   WHERE repo_name = ? AND handler = ? AND method = ? AND path = ?""",
                (repo_name, handler, method, path),
            ).fetchone()
        return dict(row) if row else None

    def list_by_repo(self, repo_name: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM api_business_rules WHERE repo_name = ? ORDER BY method, path",
                (repo_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── upsert ──

    def upsert(
        self,
        *,
        repo_name: str,
        handler: str,
        method: str,
        path: str,
        custom_prompt: str = "",
        result: str = "",
        status: str = "idle",
        error: str = "",
    ) -> int:
        now = int(time.time())
        with self._lock:
            existing = self.connection.execute(
                """SELECT id FROM api_business_rules
                   WHERE repo_name = ? AND handler = ? AND method = ? AND path = ?""",
                (repo_name, handler, method, path),
            ).fetchone()
            if existing:
                self.connection.execute(
                    """UPDATE api_business_rules
                       SET custom_prompt = ?, result = ?, status = ?, error = ?, updated_at = ?
                       WHERE id = ?""",
                    (custom_prompt, result, status, error, now, existing["id"]),
                )
                self.connection.commit()
                return existing["id"]
            cursor = self.connection.execute(
                """INSERT INTO api_business_rules
                   (repo_name, handler, method, path, custom_prompt, result, status, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (repo_name, handler, method, path, custom_prompt, result, status, error, now, now),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def update_status(self, rule_id: int, *, status: str, result: str = "", error: str = "") -> None:
        now = int(time.time())
        with self._lock:
            self.connection.execute(
                "UPDATE api_business_rules SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, result, error, now, rule_id),
            )
            self.connection.commit()

    def update_prompt(self, rule_id: int, custom_prompt: str) -> None:
        now = int(time.time())
        with self._lock:
            self.connection.execute(
                "UPDATE api_business_rules SET custom_prompt = ?, updated_at = ? WHERE id = ?",
                (custom_prompt, now, rule_id),
            )
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()
