"""SQLite persistence for external-system UI recordings and replay runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class UiTestStore:
    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.connection.executescript(
            """CREATE TABLE IF NOT EXISTS ui_test_targets (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 repository TEXT NOT NULL,
                 name TEXT NOT NULL,
                 base_url TEXT NOT NULL,
                 allowed_origins TEXT NOT NULL,
                 created_at INTEGER NOT NULL,
                 UNIQUE(repository, name)
               );
               CREATE TABLE IF NOT EXISTS ui_recordings (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 repository TEXT NOT NULL,
                 target_id INTEGER NOT NULL,
                 name TEXT NOT NULL,
                 start_url TEXT NOT NULL,
                 webbridge_session TEXT NOT NULL,
                 status TEXT NOT NULL,
                 network_log TEXT NOT NULL DEFAULT '[]',
                 created_at INTEGER NOT NULL,
                 finished_at INTEGER
               );
               CREATE TABLE IF NOT EXISTS ui_recording_steps (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 recording_id INTEGER NOT NULL,
                 sequence INTEGER NOT NULL,
                 action TEXT NOT NULL,
                 target TEXT NOT NULL DEFAULT '{}',
                 payload TEXT NOT NULL DEFAULT '{}',
                 page_url TEXT NOT NULL DEFAULT '',
                 created_at INTEGER NOT NULL
               );
               CREATE TABLE IF NOT EXISTS ui_test_runs (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 recording_id INTEGER NOT NULL,
                 status TEXT NOT NULL,
                 current_step INTEGER,
                 started_at INTEGER NOT NULL,
                 finished_at INTEGER,
                 duration_ms REAL,
                 error TEXT NOT NULL DEFAULT '',
                 screenshot_path TEXT NOT NULL DEFAULT '',
                 webbridge_session TEXT NOT NULL
               );"""
        )
        self.connection.commit()

    def add_target(self, repository: str, name: str, base_url: str, origins: list[str]) -> dict:
        with self.lock:
            cursor = self.connection.execute(
                """INSERT INTO ui_test_targets
                   (repository, name, base_url, allowed_origins, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repository, name) DO UPDATE SET
                     base_url=excluded.base_url, allowed_origins=excluded.allowed_origins""",
                (repository, name, base_url, json.dumps(origins), int(time.time())),
            )
            self.connection.commit()
            target_id = cursor.lastrowid or self.connection.execute(
                "SELECT id FROM ui_test_targets WHERE repository=? AND name=?",
                (repository, name),
            ).fetchone()[0]
        return self.get_target(target_id)

    def get_target(self, target_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM ui_test_targets WHERE id=?", (target_id,)
        ).fetchone()
        return self._target(row) if row else None

    def list_targets(self, repository: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM ui_test_targets WHERE repository=? ORDER BY name", (repository,)
        ).fetchall()
        return [self._target(row) for row in rows]

    def create_recording(
        self, repository: str, target_id: int, name: str, start_url: str, session: str
    ) -> dict:
        with self.lock:
            cursor = self.connection.execute(
                """INSERT INTO ui_recordings
                   (repository, target_id, name, start_url, webbridge_session, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'recording', ?)""",
                (repository, target_id, name, start_url, session, int(time.time())),
            )
            self.connection.commit()
        return self.get_recording(cursor.lastrowid)

    def append_steps(self, recording_id: int, actions: list[dict]) -> None:
        if not actions:
            return
        with self.lock:
            current = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM ui_recording_steps WHERE recording_id=?",
                (recording_id,),
            ).fetchone()[0]
            values = []
            for offset, action in enumerate(actions, 1):
                values.append(
                    (
                        recording_id,
                        current + offset,
                        action.get("action", "unknown"),
                        json.dumps(action.get("target") or {}, ensure_ascii=False),
                        json.dumps(
                            {
                                key: value
                                for key, value in action.items()
                                if key not in {"action", "target", "url", "timestamp"}
                            },
                            ensure_ascii=False,
                        ),
                        action.get("url", ""),
                        int(action.get("timestamp", time.time() * 1000) / 1000),
                    )
                )
            self.connection.executemany(
                """INSERT INTO ui_recording_steps
                   (recording_id, sequence, action, target, payload, page_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            self.connection.commit()

    def append_checkpoint(
        self, recording_id: int, action: str, target: dict, payload: dict, page_url: str = ""
    ) -> dict:
        with self.lock:
            sequence = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM ui_recording_steps WHERE recording_id=?",
                (recording_id,),
            ).fetchone()[0]
            self.connection.execute(
                """INSERT INTO ui_recording_steps
                   (recording_id, sequence, action, target, payload, page_url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    recording_id,
                    sequence,
                    action,
                    json.dumps(target, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    page_url,
                    int(time.time()),
                ),
            )
            self.connection.commit()
        return self.get_recording(recording_id)

    def finish_recording(self, recording_id: int, network_log: list) -> dict:
        with self.lock:
            self.connection.execute(
                """UPDATE ui_recordings SET status='recorded', network_log=?, finished_at=?
                   WHERE id=?""",
                (json.dumps(network_log, ensure_ascii=False), int(time.time()), recording_id),
            )
            self.connection.commit()
        return self.get_recording(recording_id)

    def get_recording(self, recording_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM ui_recordings WHERE id=?", (recording_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["network_log"] = json.loads(result["network_log"])
        result["steps"] = self.list_steps(recording_id)
        return result

    def list_recordings(self, repository: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT id FROM ui_recordings WHERE repository=? ORDER BY id DESC", (repository,)
        ).fetchall()
        return [self.get_recording(row["id"]) for row in rows]

    def list_steps(self, recording_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM ui_recording_steps WHERE recording_id=? ORDER BY sequence",
            (recording_id,),
        ).fetchall()
        return [
            {**dict(row), "target": json.loads(row["target"]), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def create_run(self, recording_id: int, session: str) -> dict:
        with self.lock:
            cursor = self.connection.execute(
                """INSERT INTO ui_test_runs
                   (recording_id, status, started_at, webbridge_session)
                   VALUES (?, 'running', ?, ?)""",
                (recording_id, int(time.time()), session),
            )
            self.connection.commit()
        return self.get_run(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, **values) -> dict:
        with self.lock:
            self.connection.execute(
                """UPDATE ui_test_runs SET status=?, current_step=?, finished_at=?, duration_ms=?,
                   error=?, screenshot_path=? WHERE id=?""",
                (
                    status,
                    values.get("current_step"),
                    int(time.time()),
                    values.get("duration_ms"),
                    values.get("error", ""),
                    values.get("screenshot_path", ""),
                    run_id,
                ),
            )
            self.connection.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM ui_test_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.connection.close()

    @staticmethod
    def _target(row) -> dict:
        result = dict(row)
        result["allowed_origins"] = json.loads(result["allowed_origins"])
        return result
