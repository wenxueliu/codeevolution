"""Durable storage for snapshot-scoped terminology results and reviews."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .analysis_snapshot_sqlite import utc_now


class TermStore:
    """Persist terms separately from immutable repository snapshot facts."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS terms (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    bounded_context TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    term_type TEXT NOT NULL,
                    definition TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS term_snapshot_results (
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    domain_score REAL NOT NULL DEFAULT 0,
                    confidence_score REAL NOT NULL,
                    confidence_band TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    risk_flags_json TEXT NOT NULL DEFAULT '[]',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    rank INTEGER NOT NULL DEFAULT 0,
                    reviewed_at TEXT,
                    PRIMARY KEY(snapshot_id, term_id)
                );
                CREATE INDEX IF NOT EXISTS idx_term_results_snapshot
                    ON term_snapshot_results(snapshot_id, confidence_score DESC, rank);
                CREATE TABLE IF NOT EXISTS term_evidence (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    evidence_type TEXT NOT NULL,
                    evidence_value TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    source_location_json TEXT NOT NULL DEFAULT '{}',
                    node_id TEXT,
                    rule_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_term_evidence_lookup
                    ON term_evidence(snapshot_id, term_id);
                CREATE TABLE IF NOT EXISTS term_relations (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    source_term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    target_term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    score_breakdown_json TEXT NOT NULL DEFAULT '{}',
                    llm_status TEXT NOT NULL DEFAULT 'not_requested',
                    UNIQUE(snapshot_id, source_term_id, target_term_id, relationship)
                );
                CREATE TABLE IF NOT EXISTS term_alignments (
                    id TEXT PRIMARY KEY,
                    view_id TEXT NOT NULL,
                    source_service_id TEXT NOT NULL,
                    target_service_id TEXT NOT NULL,
                    source_term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    target_term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    score_breakdown_json TEXT NOT NULL DEFAULT '{}',
                    service_relation_reasons_json TEXT NOT NULL DEFAULT '[]',
                    algorithm_version TEXT NOT NULL DEFAULT 'term-alignment/v1',
                    reviewer TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(view_id, source_term_id, target_term_id, relationship)
                );
                CREATE INDEX IF NOT EXISTS idx_term_alignments_view
                    ON term_alignments(view_id, status, confidence DESC);
                CREATE TABLE IF NOT EXISTS term_overrides (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT,
                    scope TEXT NOT NULL,
                    matcher TEXT NOT NULL,
                    action TEXT NOT NULL,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_term_overrides_snapshot
                    ON term_overrides(snapshot_id, term_id, created_at);
                """
            )

    def close(self) -> None:
        """Connections are per operation; retained for lifecycle symmetry."""

    def replace_snapshot(self, snapshot_id: str, repository_id: str, report: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("DELETE FROM term_evidence WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM term_relations WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM term_snapshot_results WHERE snapshot_id=?", (snapshot_id,))
            for term in report.get("terms", []):
                term_id = term["id"]
                connection.execute(
                    """INSERT INTO terms
                       (id,repository_id,bounded_context,canonical_name,normalized_name,term_type,definition,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name,
                       normalized_name=excluded.normalized_name,term_type=excluded.term_type,
                       definition=CASE WHEN excluded.definition != '' THEN excluded.definition ELSE terms.definition END,
                       updated_at=excluded.updated_at""",
                    (term_id, repository_id, term.get("bounded_context", repository_id), term["canonical_name"],
                     term["normalized_name"], term["term_type"], term.get("definition", ""), now, now),
                )
                connection.execute(
                    """INSERT INTO term_snapshot_results
                       (snapshot_id,term_id,domain_score,confidence_score,confidence_band,status,source,risk_flags_json,aliases_json,rank)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, term_id, term.get("domain_score", 0), term.get("confidence_score", 0),
                     term.get("confidence_band", "low"), term.get("status", "candidate"), term.get("source", "rule"),
                     json.dumps(term.get("risk_flags", []), ensure_ascii=False),
                     json.dumps(term.get("aliases", []), ensure_ascii=False), term.get("rank", 0)),
                )
                for index, evidence in enumerate(term.get("evidence", [])):
                    evidence_id = evidence.get("id") or "ev-" + sha256(
                        f"{snapshot_id}|{term_id}|{index}|{evidence.get('evidence_type')}|{evidence.get('evidence_value')}".encode()
                    ).hexdigest()[:24]
                    connection.execute(
                        """INSERT INTO term_evidence
                           (id,snapshot_id,term_id,evidence_type,evidence_value,weight,source_location_json,node_id,rule_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (evidence_id, snapshot_id, term_id, evidence.get("evidence_type", ""),
                         evidence.get("evidence_value", ""), evidence.get("weight", 0),
                         json.dumps(evidence.get("source_location", {}), ensure_ascii=False), evidence.get("node_id"),
                         evidence.get("rule_id", ""), now),
                    )
            for relation in report.get("relations", []):
                connection.execute(
                    """INSERT OR REPLACE INTO term_relations
                       (id,snapshot_id,source_term_id,target_term_id,relationship,confidence,status,score_breakdown_json,llm_status)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("rel-" + uuid4().hex, snapshot_id, relation["source_term_id"], relation["target_term_id"],
                     relation["relationship"], relation.get("confidence", 0), relation.get("status", "candidate"),
                     json.dumps(relation.get("score_breakdown", {}), ensure_ascii=False), relation.get("llm_status", "not_requested")),
                )
        return self.get_summary(snapshot_id)

    def get_summary(self, snapshot_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM term_snapshot_results WHERE snapshot_id=? GROUP BY status",
                (snapshot_id,),
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM term_snapshot_results WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0]
        counts = {row["status"]: row["count"] for row in rows}
        return {"total": total, "accepted": counts.get("accepted", 0), "candidates": total - counts.get("accepted", 0), "needs_review": counts.get("needs_review", 0)}

    def replace_alignments(self, view_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Persist recommendations while preserving prior human decisions."""
        now = utc_now()
        items = list(result.get("mappings", [])) + list(result.get("alternatives", []))
        with self._connection() as connection:
            for item in items:
                source_term_id = str(item.get("source_term_id") or "")
                target_term_id = str(item.get("target_term_id") or "")
                if not source_term_id or not target_term_id:
                    continue
                existing = connection.execute(
                    "SELECT status FROM term_alignments WHERE view_id=? AND source_term_id=? AND target_term_id=? AND relationship=?",
                    (view_id, source_term_id, target_term_id, item.get("relationship", "related")),
                ).fetchone()
                if existing and existing["status"] in {"accepted", "rejected"}:
                    continue
                alignment_id = item.get("id") or "align-" + sha256(
                    f"{view_id}|{source_term_id}|{target_term_id}|{item.get('relationship', 'related')}".encode()
                ).hexdigest()[:24]
                connection.execute(
                    """INSERT INTO term_alignments
                       (id,view_id,source_service_id,target_service_id,source_term_id,target_term_id,relationship,confidence,status,score_breakdown_json,service_relation_reasons_json,algorithm_version,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(view_id,source_term_id,target_term_id,relationship) DO UPDATE SET
                       confidence=excluded.confidence,status=excluded.status,score_breakdown_json=excluded.score_breakdown_json,
                       service_relation_reasons_json=excluded.service_relation_reasons_json,algorithm_version=excluded.algorithm_version""",
                    (alignment_id, view_id, item.get("source_service_id", ""), item.get("target_service_id", ""),
                     source_term_id, target_term_id, item.get("relationship", "related"), item.get("confidence", 0),
                     "needs_review", json.dumps(item.get("score_breakdown", {}), ensure_ascii=False),
                     json.dumps(item.get("service_relation_reasons", []), ensure_ascii=False), "term-alignment/v1", now),
                )
        return self.list_alignments(view_id)

    def list_alignments(self, view_id: str, *, status: str = "", limit: int = 500, offset: int = 0) -> dict[str, Any]:
        clauses = ["view_id=?"]
        params: list[Any] = [view_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM term_alignments WHERE {where} ORDER BY confidence DESC, id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            total = connection.execute(f"SELECT COUNT(*) FROM term_alignments WHERE {where}", tuple(params)).fetchone()[0]
        return {"view_id": view_id, "total": total, "alignments": [self._alignment_row(row) for row in rows]}

    def review_alignment(self, view_id: str, alignment_id: str, action: str, reason: str = "", author: str = "") -> dict[str, Any]:
        if action not in {"accept", "reject"}:
            raise ValueError("unsupported alignment review action")
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE term_alignments SET status=?,reviewer=?,reviewed_at=? WHERE id=? AND view_id=?",
                ("accepted" if action == "accept" else "rejected", author, now, alignment_id, view_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(alignment_id)
            connection.execute(
                "INSERT INTO term_overrides (id,snapshot_id,term_id,scope,matcher,action,value_json,reason,author,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("override-" + uuid4().hex, view_id, None, "view", alignment_id, action, "{}", reason, author, now),
            )
        result = self.list_alignments(view_id)
        return next(item for item in result["alignments"] if item["id"] == alignment_id)

    def list_terms(
        self, snapshot_id: str, *, term_type: str = "", status: str = "", confidence_band: str = "",
        name: str = "", limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        clauses = ["r.snapshot_id=?"]
        params: list[Any] = [snapshot_id]
        for column, value in (("t.term_type", term_type), ("r.status", status), ("r.confidence_band", confidence_band)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if name:
            clauses.append("(t.canonical_name LIKE ? OR t.normalized_name LIKE ?)")
            params.extend([f"%{name}%", f"%{name.lower()}%"])
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM terms t JOIN term_snapshot_results r ON r.term_id=t.id WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"""SELECT t.*,r.domain_score,r.confidence_score,r.confidence_band,r.status,r.source,
                    r.risk_flags_json,r.aliases_json,r.rank,r.reviewed_at,
                    (SELECT COUNT(*) FROM term_evidence e WHERE e.snapshot_id=r.snapshot_id AND e.term_id=t.id) AS evidence_count
                    FROM terms t JOIN term_snapshot_results r ON r.term_id=t.id
                    WHERE {where} ORDER BY r.confidence_score DESC,r.rank,t.id LIMIT ? OFFSET ?""",
                [*params, min(max(limit, 1), 500), max(offset, 0)],
            ).fetchall()
        return {"terms": [self._term_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def get_term(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT t.*,r.domain_score,r.confidence_score,r.confidence_band,r.status,r.source,
                   r.risk_flags_json,r.aliases_json,r.rank,r.reviewed_at,
                   (SELECT COUNT(*) FROM term_evidence e WHERE e.snapshot_id=r.snapshot_id AND e.term_id=t.id) AS evidence_count
                   FROM terms t JOIN term_snapshot_results r ON r.term_id=t.id
                   WHERE r.snapshot_id=? AND t.id=?""", (snapshot_id, term_id)
            ).fetchone()
        return self._term_row(row) if row else None

    def list_evidence(self, snapshot_id: str, term_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM term_evidence WHERE snapshot_id=? AND term_id=? ORDER BY weight DESC,id",
                (snapshot_id, term_id),
            ).fetchall()
        return [{
            "id": row["id"], "evidence_type": row["evidence_type"], "evidence_value": row["evidence_value"],
            "weight": row["weight"], "source_location": json.loads(row["source_location_json"]),
            "node_id": row["node_id"], "rule_id": row["rule_id"],
        } for row in rows]

    def review(self, snapshot_id: str, term_id: str, action: str, value: dict[str, Any], reason: str, author: str) -> dict[str, Any]:
        allowed = {"accept", "reject", "rename", "reclassify", "alias", "relate"}
        if action not in allowed:
            raise ValueError("unsupported term review action")
        now = utc_now()
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM term_snapshot_results WHERE snapshot_id=? AND term_id=?", (snapshot_id, term_id)
            ).fetchone()
            if exists is None:
                raise KeyError(term_id)
            if action in {"accept", "reject"}:
                status = "accepted" if action == "accept" else "rejected"
                connection.execute("UPDATE term_snapshot_results SET status=?,source='user_override',reviewed_at=? WHERE snapshot_id=? AND term_id=?", (status, now, snapshot_id, term_id))
            elif action == "rename":
                name = str(value.get("canonical_name") or "").strip()
                if not name:
                    raise ValueError("canonical_name is required")
                from ..analysis.knowledge.terms import normalize_term
                connection.execute("UPDATE terms SET canonical_name=?,normalized_name=?,updated_at=? WHERE id=?", (name, normalize_term(name), now, term_id))
                connection.execute("UPDATE term_snapshot_results SET source='user_override',reviewed_at=? WHERE snapshot_id=? AND term_id=?", (now, snapshot_id, term_id))
            elif action == "reclassify":
                term_type = str(value.get("term_type") or "").strip()
                if not term_type:
                    raise ValueError("term_type is required")
                connection.execute("UPDATE terms SET term_type=?,updated_at=? WHERE id=?", (term_type, now, term_id))
                connection.execute("UPDATE term_snapshot_results SET source='user_override',reviewed_at=? WHERE snapshot_id=? AND term_id=?", (now, snapshot_id, term_id))
            elif action in {"alias", "relate"}:
                target_id = str(value.get("target_term_id") or "").strip()
                relationship = "alias" if action == "alias" else str(value.get("relationship") or "related")
                if not target_id:
                    raise ValueError("target_term_id is required")
                connection.execute(
                    "INSERT OR REPLACE INTO term_relations (id,snapshot_id,source_term_id,target_term_id,relationship,confidence,status,score_breakdown_json,llm_status) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("rel-" + uuid4().hex, snapshot_id, term_id, target_id, relationship, 1.0, "accepted", "{}", "not_requested"),
                )
            connection.execute(
                "INSERT INTO term_overrides (id,snapshot_id,term_id,scope,matcher,action,value_json,reason,author,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("override-" + uuid4().hex, snapshot_id, term_id, "snapshot", term_id, action, json.dumps(value, ensure_ascii=False), reason, author, now),
            )
        result = self.get_term(snapshot_id, term_id)
        return result or {}

    def add_manual(self, snapshot_id: str, repository_id: str, value: dict[str, Any]) -> dict[str, Any]:
        name = str(value.get("canonical_name") or "").strip()
        if not name:
            raise ValueError("canonical_name is required")
        from ..analysis.knowledge.terms import display_name, normalize_term
        normalized = normalize_term(name)
        term_type = str(value.get("term_type") or "entity")
        bounded_context = str(value.get("bounded_context") or repository_id)
        term_id = "term-" + sha256(f"{repository_id}|{bounded_context}|{term_type}|{normalized}".encode()).hexdigest()[:24]
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO terms (id,repository_id,bounded_context,canonical_name,normalized_name,term_type,definition,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name,definition=excluded.definition,updated_at=excluded.updated_at""",
                (term_id, repository_id, bounded_context, name if name.isupper() else (display_name(name) or name), normalized, term_type, str(value.get("definition") or ""), now, now),
            )
            connection.execute(
                """INSERT OR REPLACE INTO term_snapshot_results
                   (snapshot_id,term_id,domain_score,confidence_score,confidence_band,status,source,risk_flags_json,aliases_json,rank,reviewed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, term_id, 1.0, 1.0, "high", "accepted", "manual", "[]", json.dumps(value.get("aliases", []), ensure_ascii=False), 0, now),
            )
            evidence_id = "ev-" + uuid4().hex
            connection.execute(
                "INSERT INTO term_evidence (id,snapshot_id,term_id,evidence_type,evidence_value,weight,source_location_json,node_id,rule_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, snapshot_id, term_id, "manual_definition", str(value.get("definition") or "manual term"), 1.0, "{}", None, "manual", now),
            )
            connection.execute(
                "INSERT INTO term_overrides (id,snapshot_id,term_id,scope,matcher,action,value_json,reason,author,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("override-" + uuid4().hex, snapshot_id, term_id, "snapshot", normalized, "add_term", json.dumps(value, ensure_ascii=False), str(value.get("reason") or ""), str(value.get("author") or ""), now),
            )
        return self.get_term(snapshot_id, term_id) or {}

    @staticmethod
    def _term_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["risk_flags"] = json.loads(data.pop("risk_flags_json", "[]"))
        data["aliases"] = json.loads(data.pop("aliases_json", "[]"))
        return data

    @staticmethod
    def _alignment_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["score_breakdown"] = json.loads(data.pop("score_breakdown_json", "{}"))
        data["service_relation_reasons"] = json.loads(data.pop("service_relation_reasons_json", "[]"))
        return data
