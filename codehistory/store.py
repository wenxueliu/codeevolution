"""SQLite store for evolution data."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    parent_hash TEXT,
    timestamp INTEGER NOT NULL,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    semantic_type TEXT,
    tags TEXT
);

CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT UNIQUE NOT NULL,
    canonical_name TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_signature TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL REFERENCES commits(id),
    last_seen_at INTEGER REFERENCES commits(id),
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id),
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    call_tree_nodes INTEGER NOT NULL,
    call_tree_edges INTEGER NOT NULL,
    call_tree_depth INTEGER NOT NULL,
    cyclomatic_complexity REAL,
    file_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    entry_point_node_id TEXT,
    test_nodes INTEGER DEFAULT 0,
    UNIQUE(feature_id, commit_id)
);

CREATE TABLE IF NOT EXISTS evolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id),
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    UNIQUE(feature_id, commit_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_commits_hash ON commits(hash);
CREATE INDEX IF NOT EXISTS idx_commits_timestamp ON commits(timestamp);
CREATE INDEX IF NOT EXISTS idx_features_stable_id ON features(stable_id);
CREATE INDEX IF NOT EXISTS idx_features_status ON features(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_feature ON feature_snapshots(feature_id, commit_id);
CREATE INDEX IF NOT EXISTS idx_events_feature ON evolution_events(feature_id, commit_id);
"""


class EvolutionStore:
    """SQLite-backed store for code evolution data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # --- commits ---

    def insert_commit(
        self,
        hash_: str,
        parent_hash: str | None,
        timestamp: int,
        author: str,
        message: str,
        semantic_type: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO commits (hash, parent_hash, timestamp, author, message, semantic_type, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (hash_, parent_hash, timestamp, author, message, semantic_type, json.dumps(tags) if tags else None),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM commits WHERE hash = ?", (hash_,)).fetchone()
        assert row
        return row[0]

    def get_commit_by_hash(self, hash_: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, hash, parent_hash, timestamp, author, message, semantic_type, tags FROM commits WHERE hash = ?",
            (hash_,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "hash": row[1], "parent_hash": row[2],
            "timestamp": row[3], "author": row[4], "message": row[5],
            "semantic_type": row[6], "tags": json.loads(row[7]) if row[7] else None,
        }

    def get_latest_commit_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM commits ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def get_oldest_commit_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM commits ORDER BY id ASC LIMIT 1").fetchone()
        return row[0] if row else None

    # --- features ---

    def insert_feature(
        self, stable_id: str, canonical_name: str, entry_type: str,
        entry_signature: str, first_seen_at: int,
    ) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO features (stable_id, canonical_name, entry_type, entry_signature, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (stable_id, canonical_name, entry_type, entry_signature, first_seen_at, first_seen_at),
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM features WHERE stable_id = ?", (stable_id,)).fetchone()
        assert row
        return row[0]

    def get_feature(self, stable_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, stable_id, canonical_name, entry_type, entry_signature, first_seen_at, last_seen_at, status FROM features WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "stable_id": row[1], "canonical_name": row[2],
            "entry_type": row[3], "entry_signature": row[4],
            "first_seen_at": row[5], "last_seen_at": row[6], "status": row[7],
        }

    def get_all_features(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, stable_id, canonical_name, entry_type, entry_signature, first_seen_at, last_seen_at, status FROM features WHERE status != 'removed'"
        ).fetchall()
        return [
            {"id": r[0], "stable_id": r[1], "canonical_name": r[2],
             "entry_type": r[3], "entry_signature": r[4],
             "first_seen_at": r[5], "last_seen_at": r[6], "status": r[7]}
            for r in rows
        ]

    def update_feature_last_seen(self, feature_id: int, commit_id: int):
        self.conn.execute(
            "UPDATE features SET last_seen_at = ? WHERE id = ?",
            (commit_id, feature_id),
        )
        self.conn.commit()

    def mark_feature_removed(self, feature_id: int):
        self.conn.execute(
            "UPDATE features SET status = 'removed' WHERE id = ?",
            (feature_id,),
        )
        self.conn.commit()

    # --- snapshots ---

    def insert_snapshot(self, feature_id: int, commit_id: int, snapshot_data: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO feature_snapshots
               (feature_id, commit_id, call_tree_nodes, call_tree_edges, call_tree_depth,
                cyclomatic_complexity, file_path, line_start, line_end, entry_point_node_id, test_nodes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feature_id, commit_id,
                snapshot_data["call_tree_nodes"],
                snapshot_data["call_tree_edges"],
                snapshot_data["call_tree_depth"],
                snapshot_data.get("cyclomatic_complexity"),
                snapshot_data["file_path"],
                snapshot_data["line_start"],
                snapshot_data["line_end"],
                snapshot_data.get("entry_point_node_id"),
                snapshot_data.get("test_nodes", 0),
            ),
        )
        self.conn.commit()

    def get_latest_snapshot(self, feature_id: int) -> dict | None:
        row = self.conn.execute(
            """SELECT call_tree_nodes, call_tree_edges, call_tree_depth,
                      file_path, line_start, line_end
               FROM feature_snapshots
               WHERE feature_id = ?
               ORDER BY commit_id DESC LIMIT 1""",
            (feature_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "call_tree_nodes": row[0], "call_tree_edges": row[1],
            "call_tree_depth": row[2], "file_path": row[3],
            "line_start": row[4], "line_end": row[5],
        }

    # --- events ---

    def insert_event(self, feature_id: int, commit_id: int, event_type: str, detail: dict | None = None):
        self.conn.execute(
            "INSERT OR IGNORE INTO evolution_events (feature_id, commit_id, event_type, detail) VALUES (?, ?, ?, ?)",
            (feature_id, commit_id, event_type, json.dumps(detail) if detail else None),
        )
        self.conn.commit()

    def get_feature_timeline(self, stable_id: str) -> list[dict]:
        feature = self.get_feature(stable_id)
        if not feature:
            return []
        rows = self.conn.execute(
            """SELECT e.event_type, e.detail, c.hash, c.timestamp, c.author, c.message
               FROM evolution_events e
               JOIN commits c ON e.commit_id = c.id
               WHERE e.feature_id = ?
               ORDER BY c.timestamp ASC""",
            (feature["id"],),
        ).fetchall()
        return [
            {"event_type": r[0], "detail": json.loads(r[1]) if r[1] else None,
             "commit_hash": r[2], "timestamp": r[3], "author": r[4], "message": r[5]}
            for r in rows
        ]

    # --- stats ---

    def get_stats(self) -> dict:
        stats: dict[str, Any] = {}
        row = self.conn.execute("SELECT COUNT(*) FROM commits").fetchone()
        stats["total_commits"] = row[0] if row else 0
        row = self.conn.execute("SELECT COUNT(*) FROM features").fetchone()
        stats["total_features"] = row[0] if row else 0
        row = self.conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()
        stats["total_snapshots"] = row[0] if row else 0
        row = self.conn.execute("SELECT COUNT(*) FROM evolution_events").fetchone()
        stats["total_events"] = row[0] if row else 0
        row = self.conn.execute("SELECT COUNT(*) FROM features WHERE status = 'active'").fetchone()
        stats["active_features"] = row[0] if row else 0
        return stats
