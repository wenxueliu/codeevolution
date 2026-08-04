"""Persistent audit log for assistant queries and executed read operations."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class AuditStore:
    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS assistant_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                repository TEXT NOT NULL,
                question TEXT NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            )"""
        )
        self.connection.commit()

    def record(
        self,
        *,
        repository: str,
        question: str,
        plan: list[dict],
        status: str,
        result_count: int = 0,
        duration_ms: float = 0,
        error: str = "",
    ) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO assistant_audit_logs
                   (created_at, repository, question, plan, status, result_count, duration_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(time.time()),
                    repository,
                    question,
                    json.dumps(plan, ensure_ascii=False),
                    status,
                    result_count,
                    duration_ms,
                    error,
                ),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def list(self, repository: str = "", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM assistant_audit_logs"
        params: list = []
        if repository:
            sql += " WHERE repository = ?"
            params.append(repository)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()
        return [{**dict(row), "plan": json.loads(row["plan"])} for row in rows]

    def close(self) -> None:
        self.connection.close()
