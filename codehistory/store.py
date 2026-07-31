"""SQLite store for evolution data."""

import json
import sqlite3
from contextlib import contextmanager
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
    status TEXT DEFAULT 'active',
    description TEXT DEFAULT '',
    description_zh TEXT DEFAULT ''
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
    call_chain TEXT DEFAULT '[]',
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
        self._migrate()
        self.conn.commit()
        self._transaction_depth = 0

    @contextmanager
    def transaction(self):
        """Group all writes for one analyzed commit into a single transaction."""
        outermost = self._transaction_depth == 0
        if outermost:
            self.conn.execute("BEGIN")
        self._transaction_depth += 1
        try:
            yield
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                self.conn.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self.conn.commit()

    def _commit_if_needed(self):
        if self._transaction_depth == 0:
            self.conn.commit()

    def _migrate(self):
        """Add columns that may not exist in older DBs."""
        migrations = [
            "ALTER TABLE features ADD COLUMN description TEXT DEFAULT ''",
            "ALTER TABLE features ADD COLUMN description_zh TEXT DEFAULT ''",
            "ALTER TABLE feature_snapshots ADD COLUMN call_chain TEXT DEFAULT '[]'",
        ]
        for m in migrations:
            try:
                self.conn.execute(m)
            except sqlite3.OperationalError:
                pass  # Column already exists

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
            (
                hash_,
                parent_hash,
                timestamp,
                author,
                message,
                semantic_type,
                json.dumps(tags) if tags else None,
            ),
        )
        self._commit_if_needed()
        if cur.rowcount == 1:
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
            "id": row[0],
            "hash": row[1],
            "parent_hash": row[2],
            "timestamp": row[3],
            "author": row[4],
            "message": row[5],
            "semantic_type": row[6],
            "tags": json.loads(row[7]) if row[7] else None,
        }

    def get_latest_commit_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM commits ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def get_oldest_commit_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM commits ORDER BY id ASC LIMIT 1").fetchone()
        return row[0] if row else None

    # --- features ---

    def insert_feature(
        self,
        stable_id: str,
        canonical_name: str,
        entry_type: str,
        entry_signature: str,
        first_seen_at: int,
        description: str = "",
        description_zh: str = "",
    ) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO features
               (stable_id, canonical_name, entry_type, entry_signature, first_seen_at, last_seen_at, description, description_zh)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stable_id,
                canonical_name,
                entry_type,
                entry_signature,
                first_seen_at,
                first_seen_at,
                description,
                description_zh,
            ),
        )
        self._commit_if_needed()
        if cur.rowcount == 1:
            return cur.lastrowid
        row = self.conn.execute(
            "SELECT id FROM features WHERE stable_id = ?", (stable_id,)
        ).fetchone()
        assert row
        return row[0]

    def get_feature(self, stable_id: str) -> dict | None:
        row = self.conn.execute(
            """SELECT id, stable_id, canonical_name, entry_type, entry_signature,
                      first_seen_at, last_seen_at, status, description, description_zh
               FROM features WHERE stable_id = ?""",
            (stable_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "stable_id": row[1],
            "canonical_name": row[2],
            "entry_type": row[3],
            "entry_signature": row[4],
            "first_seen_at": row[5],
            "last_seen_at": row[6],
            "status": row[7],
            "description": row[8] or "",
            "description_zh": row[9] or "",
        }

    def get_all_features(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, stable_id, canonical_name, entry_type, entry_signature,
                      first_seen_at, last_seen_at, status, description, description_zh
               FROM features WHERE status != 'removed'"""
        ).fetchall()
        return [
            {
                "id": r[0],
                "stable_id": r[1],
                "canonical_name": r[2],
                "entry_type": r[3],
                "entry_signature": r[4],
                "first_seen_at": r[5],
                "last_seen_at": r[6],
                "status": r[7],
                "description": r[8] or "",
                "description_zh": r[9] or "",
            }
            for r in rows
        ]

    def get_active_features(self) -> list[dict]:
        """Return features that should participate in matching the next commit."""
        return [feature for feature in self.get_all_features() if feature["status"] == "active"]

    def update_feature_last_seen(self, feature_id: int, commit_id: int):
        self.conn.execute(
            "UPDATE features SET last_seen_at = ? WHERE id = ?",
            (commit_id, feature_id),
        )
        self._commit_if_needed()

    def mark_feature_active(self, feature_id: int, commit_id: int):
        self.conn.execute(
            "UPDATE features SET status = 'active', last_seen_at = ? WHERE id = ?",
            (commit_id, feature_id),
        )
        self._commit_if_needed()

    def mark_feature_removed(self, feature_id: int):
        self.conn.execute(
            "UPDATE features SET status = 'removed' WHERE id = ?",
            (feature_id,),
        )
        self._commit_if_needed()

    # --- snapshots ---

    def insert_snapshot(self, feature_id: int, commit_id: int, snapshot_data: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO feature_snapshots
               (feature_id, commit_id, call_tree_nodes, call_tree_edges, call_tree_depth,
                cyclomatic_complexity, file_path, line_start, line_end, entry_point_node_id, test_nodes, call_chain)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feature_id,
                commit_id,
                snapshot_data["call_tree_nodes"],
                snapshot_data["call_tree_edges"],
                snapshot_data["call_tree_depth"],
                snapshot_data.get("cyclomatic_complexity"),
                snapshot_data["file_path"],
                snapshot_data["line_start"],
                snapshot_data["line_end"],
                snapshot_data.get("entry_point_node_id"),
                snapshot_data.get("test_nodes", 0),
                json.dumps(snapshot_data.get("call_chain", [])),
            ),
        )
        self._commit_if_needed()

    def get_latest_snapshot(self, feature_id: int) -> dict | None:
        row = self.conn.execute(
            """SELECT call_tree_nodes, call_tree_edges, call_tree_depth,
                      file_path, line_start, line_end, call_chain
               FROM feature_snapshots
               WHERE feature_id = ?
               ORDER BY commit_id DESC LIMIT 1""",
            (feature_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "call_tree_nodes": row[0],
            "call_tree_edges": row[1],
            "call_tree_depth": row[2],
            "file_path": row[3],
            "line_start": row[4],
            "line_end": row[5],
            "call_chain": json.loads(row[6]) if row[6] else [],
        }

    # --- events ---

    def insert_event(
        self, feature_id: int, commit_id: int, event_type: str, detail: dict | None = None
    ):
        self.conn.execute(
            "INSERT OR IGNORE INTO evolution_events (feature_id, commit_id, event_type, detail) VALUES (?, ?, ?, ?)",
            (feature_id, commit_id, event_type, json.dumps(detail) if detail else None),
        )
        self._commit_if_needed()

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
            {
                "event_type": r[0],
                "detail": json.loads(r[1]) if r[1] else None,
                "commit_hash": r[2],
                "timestamp": r[3],
                "author": r[4],
                "message": r[5],
            }
            for r in rows
        ]

    # --- commits ---

    def get_commits(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, hash, timestamp, author, message FROM commits ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "hash": r[1], "timestamp": r[2], "author": r[3], "message": r[4]}
            for r in rows
        ]

    def get_features_at_commit(self, commit_hash: str) -> list[dict]:
        """Get all features and their state at a specific commit."""
        commit_row = self.conn.execute(
            "SELECT id FROM commits WHERE hash = ?", (commit_hash,)
        ).fetchone()
        if not commit_row:
            return []
        target_commit_id = commit_row[0]

        rows = self.conn.execute(
            """SELECT f.id, f.stable_id, f.canonical_name, f.entry_type,
                      f.entry_signature, f.status, f.first_seen_at, f.last_seen_at,
                      COALESCE(
                          (SELECT fs.call_tree_nodes FROM feature_snapshots fs
                           WHERE fs.feature_id = f.id AND fs.commit_id <= ?
                           ORDER BY fs.commit_id DESC LIMIT 1),
                          0
                      ) as call_tree_nodes,
                      COALESCE(
                          (SELECT fs.call_chain FROM feature_snapshots fs
                           WHERE fs.feature_id = f.id AND fs.commit_id <= ?
                           ORDER BY fs.commit_id DESC LIMIT 1),
                          '[]'
                      ) as call_chain
               FROM features f
               WHERE f.first_seen_at <= ?
               ORDER BY f.canonical_name""",
            (target_commit_id, target_commit_id, target_commit_id),
        ).fetchall()

        # Keep features that weren't removed before or at the target commit
        result = []
        for r in rows:
            # Check if DIED event exists at or before target commit
            died = self.conn.execute(
                """SELECT 1 FROM evolution_events
                   WHERE feature_id = ? AND event_type = 'DIED' AND commit_id <= ?
                   LIMIT 1""",
                (r[0], target_commit_id),
            ).fetchone()

            if not died:
                result.append(
                    {
                        "id": r[0],
                        "stable_id": r[1],
                        "canonical_name": r[2],
                        "entry_type": r[3],
                        "entry_signature": r[4],
                        "status": r[5],
                        "first_seen_at": r[6],
                        "last_seen_at": r[7],
                        "call_tree_nodes": r[8],
                        "call_chain": json.loads(r[9]) if r[9] else [],
                    }
                )

        return result

    # --- capabilities (clustered features) ---

    def get_capabilities(self) -> list[dict]:
        """Cluster features into capabilities.

        Layer 1: Group by class (extracted from stable_id).
        Layer 2: Merge groups with overlapping call chain tails.
        """
        features = self.get_all_features()
        if not features:
            return []

        # Build per-feature data with class and call chain tails
        feat_data = {}
        for f in features:
            cls, mod = self._parse_class_module(f["stable_id"])
            snap = self.get_latest_snapshot(f["id"])
            chain = snap.get("call_chain", []) if snap else []
            # Collect unique callee names (the "to" end of each call edge)
            callees = set()
            for edge in chain:
                to_name = (edge.get("to") or "").replace("self.", "")
                if to_name:
                    callees.add(to_name)
            feat_data[f["stable_id"]] = {
                "feature": f,
                "class": cls,
                "module": mod,
                "callees": callees,
            }

        # Layer 1: Group by class
        class_groups: dict[str, dict] = {}
        for fid, fd in feat_data.items():
            key = fd["class"]  # e.g., "GraphStore"
            if key not in class_groups:
                class_groups[key] = {
                    "primary_class": key,
                    "modules": set(),
                    "features": [],
                    "all_callees": set(),
                }
            class_groups[key]["features"].append(fd["feature"])
            class_groups[key]["modules"].add(fd["module"])
            class_groups[key]["all_callees"].update(fd["callees"])

        # Layer 2: Merge groups with significant call chain overlap
        groups = list(class_groups.values())
        merged = True
        while merged:
            merged = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    g1, g2 = groups[i], groups[j]
                    if not g1["all_callees"] or not g2["all_callees"]:
                        continue
                    overlap = g1["all_callees"] & g2["all_callees"]
                    union = g1["all_callees"] | g2["all_callees"]
                    jaccard = len(overlap) / len(union) if union else 0
                    # Merge if Jaccard > 0.3 (shared callees)
                    if jaccard > 0.3:
                        g1["features"].extend(g2["features"])
                        g1["modules"].update(g2["modules"])
                        g1["all_callees"].update(g2["all_callees"])
                        # Update primary class to the larger one
                        g1["primary_class"] = (
                            g1["primary_class"]
                            if len(g1["features"]) >= len(g2["features"])
                            else g2["primary_class"]
                        )
                        groups.pop(j)
                        merged = True
                        break
                if merged:
                    break

        # Build result
        result = []
        for i, g in enumerate(groups):
            total_events = sum(
                len(self.get_feature_timeline(f["stable_id"])) for f in g["features"]
            )
            stats = self._capability_stats(g["features"])
            result.append(
                {
                    "id": "cap-" + str(i + 1),
                    "name": g["primary_class"],
                    "name_zh": self._capability_name_zh(g["primary_class"], list(g["modules"])),
                    "module": sorted(g["modules"])[0] if g["modules"] else "",
                    "modules": sorted(g["modules"]),
                    "feature_count": len(g["features"]),
                    "event_count": total_events,
                    "features": sorted(g["features"], key=lambda f: f["canonical_name"]),
                    "stats": stats,
                }
            )

        # Sort by feature count desc
        result.sort(key=lambda c: c["feature_count"], reverse=True)
        return result

    @staticmethod
    def _parse_class_module(stable_id: str) -> tuple[str, str]:
        """Parse class and module from stable_id.

        'server/graph.py::GraphStore.get_node' → ('GraphStore', 'server/graph.py')
        'server/incremental.py::get_db_path' → ('server/incremental.py', 'server/incremental.py')
        """
        if "::" in stable_id:
            file_part, func_part = stable_id.split("::", 1)
        else:
            file_part, func_part = stable_id, stable_id

        if "." in func_part:
            cls = func_part.rsplit(".", 1)[0]  # Before last dot = class
        else:
            cls = file_part  # No class → use file as class

        return cls, file_part

    @staticmethod
    def _capability_name_zh(primary_class: str, modules: list[str]) -> str:
        """Auto-generate Chinese capability name."""
        patterns = {
            "GraphStore": "图谱存储与查询",
            "CodeParser": "代码解析",
            "ChangeDetector": "变更检测",
            "FlowAnalyzer": "流程分析",
            "CommunityDetector": "社区检测",
            "ArchitectureView": "架构视图",
            "WikiGenerator": "Wiki 生成",
            "EmbeddingService": "向量嵌入",
            "SearchEngine": "搜索引擎",
            "ConfigManager": "配置管理",
            "MultiRepoManager": "多仓管理",
            "DaemonManager": "守护进程",
        }
        if primary_class in patterns:
            return patterns[primary_class]
        # Fallback: use module path
        for m in modules:
            parts = m.replace(".py", "").replace("/", " ").replace("_", " ").split()
            if parts:
                return " ".join(parts).title() + " 模块"
        return primary_class + " 能力"

    def _capability_stats(self, features: list[dict]) -> dict:
        """Aggregate stats for a capability."""
        total_nodes = 0
        total_depth = 0
        for f in features:
            snap = self.get_latest_snapshot(f["id"])
            if snap:
                total_nodes += snap.get("call_tree_nodes", 0)
                total_depth = max(total_depth, snap.get("call_tree_depth", 0))
        return {
            "total_call_tree_nodes": total_nodes,
            "max_call_depth": total_depth,
        }

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
