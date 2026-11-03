"""Durable storage for snapshot-scoped terminology results and reviews."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ..domain.term_types import canonical_term_type, term_type_aliases
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
                CREATE TABLE IF NOT EXISTS evidence_package (
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    analysis_version TEXT NOT NULL DEFAULT '',
                    conflict_count INTEGER NOT NULL DEFAULT 0,
                    package_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, term_id)
                );
                CREATE TABLE IF NOT EXISTS evidence_conflict (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    conflict_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'review',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS term_definition (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    canonical_name TEXT NOT NULL DEFAULT '',
                    definition TEXT NOT NULL DEFAULT '',
                    facts_json TEXT NOT NULL DEFAULT '[]',
                    inferences_json TEXT NOT NULL DEFAULT '[]',
                    unknowns_json TEXT NOT NULL DEFAULT '[]',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0,
                    grounding_json TEXT NOT NULL DEFAULT '{}',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    analysis_version TEXT NOT NULL DEFAULT '',
                    input_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS term_pair_judgment (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    view_id TEXT NOT NULL DEFAULT '',
                    source_term_id TEXT NOT NULL,
                    target_term_id TEXT NOT NULL,
                    judgment TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    support_evidence_json TEXT NOT NULL DEFAULT '[]',
                    against_evidence_json TEXT NOT NULL DEFAULT '[]',
                    constraints_json TEXT NOT NULL DEFAULT '[]',
                    algorithm_version TEXT NOT NULL DEFAULT 'pair-v1',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    reviewer TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(snapshot_id, view_id, source_term_id, target_term_id)
                );
                CREATE TABLE IF NOT EXISTS merge_operation (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    source_term_ids_json TEXT NOT NULL DEFAULT '[]',
                    target_term_id TEXT NOT NULL,
                    operation TEXT NOT NULL DEFAULT 'MERGE',
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    affected_relations_json TEXT NOT NULL DEFAULT '[]',
                    reviewer TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    reversed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_record (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    release_id TEXT NOT NULL DEFAULT '',
                    item_type TEXT NOT NULL DEFAULT 'TERM',
                    item_id TEXT NOT NULL,
                    item_version INTEGER NOT NULL DEFAULT 1,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    reviewer TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_snapshot (
                    release_id TEXT PRIMARY KEY,
                    release_name TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    snapshot_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_item (
                    release_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_version INTEGER NOT NULL DEFAULT 1,
                    snapshot_content_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(release_id, item_type, item_id)
                );
                CREATE TABLE IF NOT EXISTS analysis_run (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL DEFAULT '',
                    analysis_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_run_idempotent
                    ON analysis_run(repository_id, commit_id, analysis_version, input_hash);
                CREATE TABLE IF NOT EXISTS bounded_context (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'proposed',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_inventory (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    locator_json TEXT NOT NULL DEFAULT '{}',
                    source_version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_columns(
                connection,
                "terms",
                {
                    "display_name": "TEXT NOT NULL DEFAULT ''",
                    "domain_id": "TEXT NOT NULL DEFAULT ''",
                    "source_of_truth": "TEXT NOT NULL DEFAULT ''",
                    "term_confidence": "REAL NOT NULL DEFAULT 0",
                    "definition_confidence": "REAL NOT NULL DEFAULT 0",
                    "owner": "TEXT NOT NULL DEFAULT ''",
                    "steward": "TEXT NOT NULL DEFAULT ''",
                    "lifecycle_status": "TEXT NOT NULL DEFAULT 'DISCOVERED'",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "effective_time": "TEXT NOT NULL DEFAULT ''",
                    "deprecated_reason": "TEXT NOT NULL DEFAULT ''",
                    "replaced_by": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                connection,
                "term_snapshot_results",
                {
                    "priority": "TEXT NOT NULL DEFAULT 'P1'",
                    "language": "TEXT NOT NULL DEFAULT ''",
                    "module": "TEXT NOT NULL DEFAULT ''",
                    "term_confidence": "REAL NOT NULL DEFAULT 0",
                    "definition_confidence": "REAL NOT NULL DEFAULT 0",
                    "lifecycle_status": "TEXT NOT NULL DEFAULT 'DISCOVERED'",
                    "source_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )
            self._ensure_columns(
                connection,
                "term_evidence",
                {
                    "polarity": "TEXT NOT NULL DEFAULT 'SUPPORT'",
                    "reliability": "REAL NOT NULL DEFAULT 0",
                    "independence_group": "TEXT NOT NULL DEFAULT ''",
                    "content_hash": "TEXT NOT NULL DEFAULT ''",
                    "derived_from": "TEXT NOT NULL DEFAULT ''",
                    "source_version": "TEXT NOT NULL DEFAULT ''",
                    "observed_at": "TEXT NOT NULL DEFAULT ''",
                    "subject_id": "TEXT NOT NULL DEFAULT ''",
                    "predicate": "TEXT NOT NULL DEFAULT ''",
                    "object_value": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                connection,
                "term_relations",
                {
                    "direction": "TEXT NOT NULL DEFAULT 'forward'",
                    "cardinality": "TEXT NOT NULL DEFAULT ''",
                    "bounded_context_id": "TEXT NOT NULL DEFAULT ''",
                    "review_status": "TEXT NOT NULL DEFAULT 'proposed'",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "evidence_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                    "effective_time": "TEXT NOT NULL DEFAULT ''",
                },
            )
            self._ensure_columns(
                connection,
                "term_definition",
                {"payload_json": "TEXT NOT NULL DEFAULT '{}'"},
            )
            self._ensure_columns(
                connection,
                "release_snapshot",
                {"snapshot_id": "TEXT NOT NULL DEFAULT ''", "previous_release_id": "TEXT NOT NULL DEFAULT ''"},
            )

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        """Connections are per operation; retained for lifecycle symmetry."""

    def replace_snapshot(self, snapshot_id: str, repository_id: str, report: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("DELETE FROM term_evidence WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM term_relations WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM term_snapshot_results WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM asset_inventory WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM evidence_package WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM evidence_conflict WHERE snapshot_id=?", (snapshot_id,))
            connection.execute(
                "DELETE FROM term_pair_judgment WHERE snapshot_id=? AND status='proposed'",
                (snapshot_id,),
            )
            for asset in (report.get("asset_inventory") or {}).get("assets", []):
                asset_type = str(asset.get("asset_type") or "")
                name = str(asset.get("name") or "")
                if not asset_type or not name:
                    continue
                connection.execute(
                    """INSERT OR REPLACE INTO asset_inventory
                       (id,snapshot_id,asset_type,name,locator_json,source_version,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("asset-" + sha256(f"{snapshot_id}|{asset_type}|{name}".encode()).hexdigest()[:24],
                     snapshot_id, asset_type, name,
                     json.dumps(asset.get("locator", {}), ensure_ascii=False),
                     asset.get("source_version", ""), now),
                )
            conflicts_by_term: dict[str, list[dict[str, Any]]] = {}
            for conflict in report.get("conflicts", []):
                term_id = str(conflict.get("term_id") or "")
                if not term_id:
                    continue
                conflicts_by_term.setdefault(term_id, []).append(conflict)
                connection.execute(
                    """INSERT OR REPLACE INTO evidence_conflict
                       (id,snapshot_id,term_id,conflict_type,severity,detail_json,resolved,created_at)
                       VALUES(?,?,?,?,?,?,0,?)""",
                    (conflict.get("id") or "cf-" + uuid4().hex, snapshot_id, term_id,
                     conflict.get("conflict_type", ""), conflict.get("severity", "review"),
                     json.dumps(conflict.get("detail", {}), ensure_ascii=False), now),
                )
            for term in report.get("terms", []):
                term_id = term["id"]
                connection.execute(
                    """INSERT INTO terms
                       (id,repository_id,bounded_context,canonical_name,display_name,normalized_name,term_type,definition,
                        domain_id,source_of_truth,term_confidence,definition_confidence,owner,steward,lifecycle_status,version,
                        created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET canonical_name=excluded.canonical_name,
                       display_name=excluded.display_name,normalized_name=excluded.normalized_name,
                       term_type=excluded.term_type,
                       definition=CASE WHEN excluded.definition != '' THEN excluded.definition ELSE terms.definition END,
                       domain_id=excluded.domain_id,source_of_truth=excluded.source_of_truth,
                       term_confidence=excluded.term_confidence,definition_confidence=excluded.definition_confidence,
                       lifecycle_status=excluded.lifecycle_status,version=excluded.version,
                       updated_at=excluded.updated_at""",
                    (term_id, repository_id, term.get("bounded_context", repository_id), term["canonical_name"],
                     term.get("display_name") or term["canonical_name"], term["normalized_name"], term["term_type"],
                     term.get("definition", ""), term.get("domain_id") or repository_id,
                     term.get("source_of_truth", ""), term.get("term_confidence") or term.get("confidence_score", 0),
                     term.get("definition_confidence", 0), term.get("owner", ""), term.get("steward", ""),
                     term.get("lifecycle_status", "DISCOVERED"), term.get("version", 1), now, now),
                )
                connection.execute(
                    """INSERT INTO term_snapshot_results
                       (snapshot_id,term_id,domain_score,confidence_score,confidence_band,status,source,
                        risk_flags_json,aliases_json,rank,priority,language,module,term_confidence,
                        definition_confidence,lifecycle_status,source_refs_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, term_id, term.get("domain_score", 0), term.get("confidence_score", 0),
                     term.get("confidence_band", "low"), term.get("status", "candidate"), term.get("source", "rule"),
                     json.dumps(term.get("risk_flags", []), ensure_ascii=False),
                     json.dumps(term.get("aliases", []), ensure_ascii=False), term.get("rank", 0),
                     term.get("priority", "P1"), term.get("language", ""), term.get("module", ""),
                     term.get("term_confidence") or term.get("confidence_score", 0),
                     term.get("definition_confidence", 0), term.get("lifecycle_status", "DISCOVERED"),
                     json.dumps(term.get("source_refs", []), ensure_ascii=False)),
                )
                for index, evidence in enumerate(term.get("evidence", [])):
                    evidence_id = evidence.get("id") or "ev-" + sha256(
                        f"{snapshot_id}|{term_id}|{index}|{evidence.get('evidence_type')}|{evidence.get('evidence_value')}".encode()
                    ).hexdigest()[:24]
                    connection.execute(
                        """INSERT INTO term_evidence
                           (id,snapshot_id,term_id,evidence_type,evidence_value,weight,source_location_json,node_id,rule_id,
                            polarity,reliability,independence_group,content_hash,derived_from,source_version,
                            observed_at,subject_id,predicate,object_value,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (evidence_id, snapshot_id, term_id, evidence.get("evidence_type", ""),
                         evidence.get("evidence_value", ""), evidence.get("weight", 0),
                         json.dumps(evidence.get("source_location", {}), ensure_ascii=False), evidence.get("node_id"),
                         evidence.get("rule_id", ""), evidence.get("polarity", "SUPPORT"),
                         evidence.get("reliability", 0), evidence.get("independence_group", ""),
                         evidence.get("content_hash", ""), evidence.get("derived_from", ""),
                         evidence.get("source_version", ""), evidence.get("observed_at", ""),
                         evidence.get("subject_id", "") or term_id, evidence.get("predicate", ""),
                         evidence.get("object_value", ""), now),
                    )
                support = [item for item in term.get("evidence", []) if item.get("polarity", "SUPPORT") == "SUPPORT"]
                against = [item for item in term.get("evidence", []) if item.get("polarity") == "AGAINST"]
                package = {
                    "term": {key: term.get(key) for key in (
                        "id", "canonical_name", "display_name", "term_type", "priority",
                        "module", "language", "term_confidence", "decision_band", "status",
                    )},
                    "bounded_context": term.get("bounded_context", repository_id),
                    "support": support,
                    "against": against,
                    "independence_groups": sorted({
                        item.get("independence_group", "") for item in support + against
                    } - {""}),
                    "conflicts": conflicts_by_term.get(term_id, []),
                    "source_version": snapshot_id,
                    "analysis_version": report.get("analysis_version", ""),
                }
                connection.execute(
                    """INSERT OR REPLACE INTO evidence_package
                       (snapshot_id,term_id,source_version,analysis_version,conflict_count,package_json,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (snapshot_id, term_id, snapshot_id, report.get("analysis_version", ""),
                     len(package["conflicts"]),
                     json.dumps(package, ensure_ascii=False), now),
                )
            for relation in report.get("relations", []):
                connection.execute(
                    """INSERT OR REPLACE INTO term_relations
                       (id,snapshot_id,source_term_id,target_term_id,relationship,confidence,status,
                        score_breakdown_json,llm_status,direction,cardinality,bounded_context_id,review_status,version,
                        evidence_refs_json,effective_time)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("rel-" + uuid4().hex, snapshot_id, relation["source_term_id"], relation["target_term_id"],
                     relation["relationship"], relation.get("confidence", 0), relation.get("status", "candidate"),
                     json.dumps(relation.get("score_breakdown", {}), ensure_ascii=False),
                     relation.get("llm_status", "not_requested"), relation.get("direction", "forward"),
                     relation.get("cardinality", ""), relation.get("bounded_context_id", ""),
                     relation.get("review_status", "proposed"), relation.get("version", 1),
                     json.dumps(relation.get("evidence_refs", []), ensure_ascii=False),
                     relation.get("effective_time", "")),
                )
        return self.get_summary(snapshot_id)

    def apply_overrides(self, snapshot_id: str, repository_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Re-apply prior human decisions to a freshly extracted report.

        Extraction replaces a snapshot's derived rows wholesale, so decisions
        stored in ``term_overrides`` would otherwise be lost on the next run.
        """
        terms = {item["id"]: item for item in report.get("terms", [])}
        relations = report.setdefault("relations", [])
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT term_id,action,value_json FROM term_overrides "
                "WHERE snapshot_id=? ORDER BY created_at,rowid",
                (snapshot_id,),
            ).fetchall()
        for row in rows:
            action = row["action"]
            value = json.loads(row["value_json"] or "{}")
            if action == "add_term":
                self._add_override_term(report, terms, repository_id, value)
                continue
            term = terms.get(row["term_id"])
            if term is None:
                continue
            if action in {"accept", "reject"}:
                accepted = action == "accept"
                term["status"] = "accepted" if accepted else "rejected"
                term["lifecycle_status"] = "APPROVED" if accepted else "REJECTED"
                term["source"] = "user_override"
            elif action == "rename":
                name = str(value.get("canonical_name") or "").strip()
                if name:
                    from ..analysis.knowledge.terms import normalize_term

                    term["canonical_name"] = name
                    term["display_name"] = name
                    term["normalized_name"] = normalize_term(name)
                    term["source"] = "user_override"
            elif action == "reclassify":
                term_type = canonical_term_type(str(value.get("term_type") or ""))
                if term_type:
                    term["term_type"] = term_type
                    term["source"] = "user_override"
            elif action in {"alias", "relate"}:
                target_id = str(value.get("target_term_id") or "").strip()
                if target_id:
                    relationship = "alias" if action == "alias" else str(value.get("relationship") or "related")
                    if not any(
                        item.get("source_term_id") == row["term_id"]
                        and item.get("target_term_id") == target_id
                        and item.get("relationship") == relationship
                        for item in relations
                    ):
                        relations.append({
                            "source_term_id": row["term_id"], "target_term_id": target_id,
                            "relationship": relationship, "confidence": 1.0,
                            "status": "accepted", "score_breakdown": {},
                            "llm_status": "not_requested",
                        })
        self._reserialize_report(report)
        return report

    @staticmethod
    def _add_override_term(
        report: dict[str, Any], terms: dict[str, dict[str, Any]], repository_id: str, value: dict[str, Any]
    ) -> None:
        from ..analysis.knowledge.terms import display_name, normalize_term

        name = str(value.get("canonical_name") or "").strip()
        if not name:
            return
        normalized = normalize_term(name)
        term_type = canonical_term_type(str(value.get("term_type") or "entity"))
        bounded_context = str(value.get("bounded_context") or repository_id)
        term_id = "term-" + sha256(
            f"{repository_id}|{bounded_context}|{term_type}|{normalized}".encode()
        ).hexdigest()[:24]
        if term_id in terms:
            terms[term_id]["status"] = "accepted"
            return
        term = {
            "id": term_id, "repository_id": repository_id, "snapshot_id": report.get("snapshot_id", ""),
            "canonical_name": name if name.isupper() else (display_name(name) or name),
            "display_name": name, "normalized_name": normalized, "term_type": term_type,
            "bounded_context": bounded_context,
            "domain_id": repository_id, "module": "", "language": "", "priority": "P0",
            "definition": str(value.get("definition") or ""),
            "domain_score": 1.0, "confidence_score": 1.0, "confidence_band": "high",
            "term_confidence": 1.0, "definition_confidence": 1.0,
            "status": "accepted", "lifecycle_status": "APPROVED", "version": 1,
            "source": "manual", "mentions": [], "aliases": list(value.get("aliases") or []),
            "risk_flags": [], "source_refs": [],
            "evidence": [{
                "evidence_type": "manual_definition",
                "evidence_value": str(value.get("definition") or "manual term"),
                "weight": 1.0, "source_location": {}, "node_id": None, "rule_id": "manual",
            }],
        }
        terms[term_id] = term
        report.setdefault("terms", []).append(term)

    @staticmethod
    def _reserialize_report(report: dict[str, Any]) -> None:
        terms = report.setdefault("terms", [])
        terms.sort(key=lambda item: (-float(item.get("confidence_score") or 0), item.get("term_type", ""), item["id"]))
        for index, term in enumerate(terms, 1):
            term["rank"] = index
        report["default_terms"] = [item for item in terms if item.get("status") == "accepted"]
        report["candidates"] = [item for item in terms if item.get("status") != "accepted"]
        report["counts"] = {
            "total": len(terms),
            "accepted": len(report["default_terms"]),
            "candidates": len(report["candidates"]),
            "needs_review": sum(item.get("status") == "needs_review" for item in report["candidates"]),
        }

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
            self._record_review(
                connection, snapshot_id=view_id, item_type="ALIGNMENT", item_id=alignment_id,
                item_version=1, action=action, reason=reason, reviewer=author,
            )
        result = self.list_alignments(view_id)
        return next(item for item in result["alignments"] if item["id"] == alignment_id)

    def list_terms(
        self, snapshot_id: str, *, term_type: str = "", status: str = "", confidence_band: str = "",
        name: str = "", limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        clauses = ["r.snapshot_id=?"]
        params: list[Any] = [snapshot_id]
        if term_type:
            aliases = sorted(term_type_aliases(term_type))
            clauses.append(f"LOWER(t.term_type) IN ({','.join('?' for _ in aliases)})")
            params.extend(item.lower() for item in aliases)
        for column, value in (("r.status", status), ("r.confidence_band", confidence_band)):
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
                    r.risk_flags_json,r.aliases_json,r.rank,r.reviewed_at,r.priority,r.language,r.module,
                    r.term_confidence,r.definition_confidence,r.lifecycle_status,r.source_refs_json,
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
                   r.risk_flags_json,r.aliases_json,r.rank,r.reviewed_at,r.priority,r.language,r.module,
                   r.term_confidence,r.definition_confidence,r.lifecycle_status,r.source_refs_json,
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
            "polarity": row["polarity"], "reliability": row["reliability"],
            "independence_group": row["independence_group"], "content_hash": row["content_hash"],
            "derived_from": row["derived_from"], "source_version": row["source_version"],
            "observed_at": row["observed_at"], "subject_id": row["subject_id"],
            "predicate": row["predicate"], "object_value": row["object_value"],
        } for row in rows]

    def get_evidence_package(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT package_json FROM evidence_package WHERE snapshot_id=? AND term_id=?",
                (snapshot_id, term_id),
            ).fetchone()
        return json.loads(row["package_json"]) if row else None

    def list_conflicts(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_conflict WHERE snapshot_id=? ORDER BY created_at,id",
                (snapshot_id,),
            ).fetchall()
        return [{
            "id": row["id"], "term_id": row["term_id"],
            "conflict_type": row["conflict_type"], "severity": row["severity"],
            "detail": json.loads(row["detail_json"]), "resolved": bool(row["resolved"]),
        } for row in rows]

    def list_assets(self, snapshot_id: str, asset_type: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM asset_inventory WHERE snapshot_id=?"
        params: list[Any] = [snapshot_id]
        if asset_type:
            query += " AND asset_type=?"
            params.append(asset_type)
        with self._connection() as connection:
            rows = connection.execute(query + " ORDER BY asset_type,name", params).fetchall()
        return [{
            "id": row["id"], "asset_type": row["asset_type"], "name": row["name"],
            "locator": json.loads(row["locator_json"]), "source_version": row["source_version"],
        } for row in rows]

    @staticmethod
    def _record_review(
        connection: sqlite3.Connection, *, snapshot_id: str, item_type: str, item_id: str,
        item_version: int, action: str, reason: str, reviewer: str, role: str = "",
        release_id: str = "",
    ) -> None:
        connection.execute(
            """INSERT INTO review_record
               (id,snapshot_id,release_id,item_type,item_id,item_version,action,reason,reviewer,role,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("review-" + uuid4().hex, snapshot_id, release_id, item_type, item_id,
             item_version, action, reason, reviewer, role, utc_now()),
        )

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
                connection.execute(
                    "UPDATE terms SET lifecycle_status=?,version=version+1,updated_at=? WHERE id=?",
                    ("APPROVED" if action == "accept" else "REJECTED", now, term_id),
                )
            elif action == "rename":
                name = str(value.get("canonical_name") or "").strip()
                if not name:
                    raise ValueError("canonical_name is required")
                from ..analysis.knowledge.terms import normalize_term
                connection.execute("UPDATE terms SET canonical_name=?,normalized_name=?,updated_at=? WHERE id=?", (name, normalize_term(name), now, term_id))
                connection.execute("UPDATE term_snapshot_results SET source='user_override',reviewed_at=? WHERE snapshot_id=? AND term_id=?", (now, snapshot_id, term_id))
            elif action == "reclassify":
                term_type = canonical_term_type(str(value.get("term_type") or "").strip())
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
            current = connection.execute(
                "SELECT version FROM terms WHERE id=?", (term_id,)
            ).fetchone()
            self._record_review(
                connection, snapshot_id=snapshot_id, item_type="TERM", item_id=term_id,
                item_version=int((current or {"version": 1})["version"]), action=action,
                reason=reason, reviewer=author, role=str(value.get("role") or ""),
            )
        result = self.get_term(snapshot_id, term_id)
        return result or {}

    def add_manual(self, snapshot_id: str, repository_id: str, value: dict[str, Any]) -> dict[str, Any]:
        name = str(value.get("canonical_name") or "").strip()
        if not name:
            raise ValueError("canonical_name is required")
        from ..analysis.knowledge.terms import display_name, normalize_term
        normalized = normalize_term(name)
        term_type = canonical_term_type(str(value.get("term_type") or "entity"))
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

    # ── Concept normalization: pair judgments and merges ──

    def replace_pair_judgments(
        self, snapshot_id: str, view_id: str, judgments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Persist matcher output without overwriting human decisions."""
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM term_pair_judgment WHERE snapshot_id=? AND view_id=? AND status='proposed'",
                (snapshot_id, view_id),
            )
            for item in judgments:
                existing = connection.execute(
                    "SELECT status FROM term_pair_judgment WHERE snapshot_id=? AND view_id=? AND source_term_id=? AND target_term_id=?",
                    (snapshot_id, view_id, item["source_term_id"], item["target_term_id"]),
                ).fetchone()
                if existing and existing["status"] in {"accepted", "rejected"}:
                    continue
                connection.execute(
                    """INSERT INTO term_pair_judgment
                       (id,snapshot_id,view_id,source_term_id,target_term_id,judgment,confidence,
                        support_evidence_json,against_evidence_json,constraints_json,algorithm_version,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(snapshot_id,view_id,source_term_id,target_term_id) DO UPDATE SET
                       judgment=excluded.judgment,confidence=excluded.confidence,
                       support_evidence_json=excluded.support_evidence_json,
                       against_evidence_json=excluded.against_evidence_json,
                       constraints_json=excluded.constraints_json,
                       algorithm_version=excluded.algorithm_version""",
                    (item["id"], snapshot_id, view_id, item["source_term_id"], item["target_term_id"],
                     item["judgment"], item["confidence"],
                     json.dumps(item.get("support_evidence", []), ensure_ascii=False),
                     json.dumps(item.get("against_evidence", []), ensure_ascii=False),
                     json.dumps(item.get("constraints", []), ensure_ascii=False),
                     item.get("algorithm_version", "pair-v1"), "proposed", now),
                )
        return self.list_pair_judgments(snapshot_id=snapshot_id, view_id=view_id)

    def list_pair_judgments(
        self, *, snapshot_id: str = "", view_id: str = "", status: str = "",
        judgment: str = "", limit: int = 500, offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if snapshot_id:
            clauses.append("snapshot_id=?")
            params.append(snapshot_id)
        if view_id:
            clauses.append("view_id=?")
            params.append(view_id)
        for column, value in (("status", status), ("judgment", judgment)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " AND ".join(clauses) if clauses else "1=1"
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM term_pair_judgment WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM term_pair_judgment WHERE {where} ORDER BY confidence DESC,id LIMIT ? OFFSET ?",
                (*params, min(max(limit, 1), 1000), max(offset, 0)),
            ).fetchall()
        return {
            "total": total,
            "judgments": [self._pair_row(row) for row in rows],
            "snapshot_id": snapshot_id, "view_id": view_id,
        }

    def review_pair_judgment(
        self, judgment_id: str, action: str, reason: str = "", reviewer: str = "",
        role: str = "", snapshot_id: str = "", view_id: str = "",
    ) -> dict[str, Any]:
        if action not in {"accept", "reject"}:
            raise ValueError("unsupported pair judgment review action")
        now = utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM term_pair_judgment WHERE id=?", (judgment_id,)
            ).fetchone()
            if row is None:
                raise KeyError(judgment_id)
            connection.execute(
                "UPDATE term_pair_judgment SET status=?,reviewer=?,reviewed_at=? WHERE id=?",
                ("accepted" if action == "accept" else "rejected", reviewer, now, judgment_id),
            )
            self._record_review(
                connection, snapshot_id=str(row["snapshot_id"] or view_id),
                item_type="PAIR_JUDGMENT", item_id=judgment_id, item_version=1,
                action=action, reason=reason, reviewer=reviewer, role=role,
            )
        return self._pair_row(self._fetch_one("term_pair_judgment", judgment_id))

    def merge_terms(
        self, snapshot_id: str, source_term_ids: list[str], target_term_id: str,
        *, reason: str = "", reviewer: str = "", role: str = "",
        evidence_refs: list[str] | None = None, confirm_hard_constraints: bool = False,
    ) -> dict[str, Any]:
        """Merge terms in one transaction, keeping a reversible audit record."""
        sources = [item for item in dict.fromkeys(source_term_ids) if item and item != target_term_id]
        if not sources:
            raise ValueError("source_term_ids must contain at least one other term")
        now = utc_now()
        with self._connection() as connection:
            target = connection.execute(
                "SELECT * FROM terms WHERE id=?", (target_term_id,)
            ).fetchone()
            if target is None:
                raise KeyError(target_term_id)
            source_rows = []
            for term_id in sources:
                row = connection.execute("SELECT * FROM terms WHERE id=?", (term_id,)).fetchone()
                if row is None:
                    raise KeyError(term_id)
                source_rows.append(row)
            # Terms are global rows linked to snapshots via term_snapshot_results;
            # a merge may only touch terms that belong to the requested snapshot.
            term_ids = [target_term_id, *sources]
            placeholders = ",".join("?" for _ in term_ids)
            linked = {
                row["term_id"] for row in connection.execute(
                    f"SELECT term_id FROM term_snapshot_results WHERE snapshot_id=? AND term_id IN ({placeholders})",
                    (snapshot_id, *term_ids),
                )
            }
            for term_id in term_ids:
                if term_id not in linked:
                    raise KeyError(term_id)
            hard = self._hard_constraints(connection, sources, target_term_id)
            if hard and not confirm_hard_constraints:
                raise ValueError(
                    "merge blocked by hard constraints: "
                    + ", ".join(sorted({item["code"] for item in hard}))
                )
            relation_rows = connection.execute(
                f"""SELECT * FROM term_relations WHERE snapshot_id=?
                    AND (source_term_id IN ({','.join('?' for _ in sources)})
                         OR target_term_id IN ({','.join('?' for _ in sources)}))""",
                (snapshot_id, *sources, *sources),
            ).fetchall()
            before = [dict(row) for row in relation_rows]
            connection.execute(
                f"DELETE FROM term_relations WHERE snapshot_id=? AND "
                f"(source_term_id IN ({','.join('?' for _ in sources)}) "
                f"OR target_term_id IN ({','.join('?' for _ in sources)}))",
                (snapshot_id, *sources, *sources),
            )
            migrated = []
            for row in before:
                source_id = target_term_id if row["source_term_id"] in sources else row["source_term_id"]
                target_id = target_term_id if row["target_term_id"] in sources else row["target_term_id"]
                if source_id == target_id:
                    continue
                new_id = "rel-" + uuid4().hex
                connection.execute(
                    """INSERT OR IGNORE INTO term_relations
                       (id,snapshot_id,source_term_id,target_term_id,relationship,confidence,status,
                        score_breakdown_json,llm_status,direction,cardinality,bounded_context_id,
                        review_status,version,evidence_refs_json,effective_time)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id, row["snapshot_id"], source_id, target_id, row["relationship"],
                     row["confidence"], row["status"], row["score_breakdown_json"],
                     row["llm_status"], row["direction"], row["cardinality"],
                     row["bounded_context_id"], row["review_status"], row["version"],
                     row["evidence_refs_json"], row["effective_time"]),
                )
                migrated.append({**dict(row), "id": new_id, "source_term_id": source_id,
                                 "target_term_id": target_id})
            for term_id in sources:
                connection.execute(
                    "UPDATE terms SET lifecycle_status='MERGED',replaced_by=?,version=version+1,updated_at=? WHERE id=?",
                    (target_term_id, now, term_id),
                )
                connection.execute(
                    "UPDATE term_snapshot_results SET lifecycle_status='MERGED' WHERE snapshot_id=? AND term_id=?",
                    (snapshot_id, term_id),
                )
            connection.execute(
                "UPDATE terms SET version=version+1,updated_at=? WHERE id=?",
                (now, target_term_id),
            )
            merge_id = "merge-" + uuid4().hex
            affected = {
                "before": before,
                "after": migrated,
                "terms": [{key: row[key] for key in ("id", "canonical_name", "lifecycle_status", "version")}
                          for row in source_rows],
            }
            connection.execute(
                """INSERT INTO merge_operation
                   (id,snapshot_id,source_term_ids_json,target_term_id,operation,reason,
                    evidence_refs_json,affected_relations_json,reviewer,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (merge_id, snapshot_id, json.dumps(sources), target_term_id, "MERGE",
                 reason, json.dumps(evidence_refs or []), json.dumps(affected, ensure_ascii=False),
                 reviewer, "applied", now),
            )
            self._record_review(
                connection, snapshot_id=snapshot_id, item_type="MERGE", item_id=merge_id,
                item_version=1, action="merge", reason=reason, reviewer=reviewer, role=role,
            )
        return self._merge_row(self._fetch_one("merge_operation", merge_id))

    def reverse_merge(
        self, merge_id: str, *, reason: str = "", reviewer: str = "", role: str = "",
    ) -> dict[str, Any]:
        """Compensate a merge; the original record is kept and marked reversed."""
        now = utc_now()
        with self._connection() as connection:
            merge = connection.execute(
                "SELECT * FROM merge_operation WHERE id=?", (merge_id,)
            ).fetchone()
            if merge is None:
                raise KeyError(merge_id)
            if merge["operation"] != "MERGE" or merge["status"] != "applied":
                raise ValueError("only an applied MERGE can be reversed")
            affected = json.loads(merge["affected_relations_json"] or "{}")
            after = affected.get("after", [])
            if after:
                connection.execute(
                    f"DELETE FROM term_relations WHERE id IN ({','.join('?' for _ in after)})",
                    tuple(row["id"] for row in after),
                )
            for row in affected.get("before", []):
                connection.execute(
                    """INSERT OR IGNORE INTO term_relations
                       (id,snapshot_id,source_term_id,target_term_id,relationship,confidence,status,
                        score_breakdown_json,llm_status,direction,cardinality,bounded_context_id,
                        review_status,version,evidence_refs_json,effective_time)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row.get("id") or "rel-" + uuid4().hex, row["snapshot_id"],
                     row["source_term_id"], row["target_term_id"], row["relationship"],
                     row["confidence"], row["status"], row["score_breakdown_json"],
                     row["llm_status"], row["direction"], row["cardinality"],
                     row["bounded_context_id"], row["review_status"], row["version"],
                     row["evidence_refs_json"], row["effective_time"]),
                )
            for term in affected.get("terms", []):
                connection.execute(
                    "UPDATE terms SET lifecycle_status=?,replaced_by='',version=version+1,updated_at=? WHERE id=?",
                    (term.get("lifecycle_status") or "DISCOVERED", now, term["id"]),
                )
                connection.execute(
                    "UPDATE term_snapshot_results SET lifecycle_status=? WHERE snapshot_id=? AND term_id=?",
                    (term.get("lifecycle_status") or "DISCOVERED", merge["snapshot_id"], term["id"]),
                )
            reverse_id = "merge-" + uuid4().hex
            connection.execute(
                """INSERT INTO merge_operation
                   (id,snapshot_id,source_term_ids_json,target_term_id,operation,reason,
                    evidence_refs_json,affected_relations_json,reviewer,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (reverse_id, merge["snapshot_id"], merge["source_term_ids_json"],
                 merge["target_term_id"], "REVERSE_MERGE", reason, "[]",
                 json.dumps(affected, ensure_ascii=False), reviewer, "applied", now),
            )
            connection.execute(
                "UPDATE merge_operation SET status='reversed',reversed_by=? WHERE id=?",
                (reverse_id, merge_id),
            )
            self._record_review(
                connection, snapshot_id=merge["snapshot_id"], item_type="MERGE",
                item_id=reverse_id, item_version=1, action="reverse_merge",
                reason=reason, reviewer=reviewer, role=role,
            )
        return self._merge_row(self._fetch_one("merge_operation", reverse_id))

    def list_merges(self, snapshot_id: str = "", limit: int = 200) -> dict[str, Any]:
        query = "SELECT * FROM merge_operation"
        params: list[Any] = []
        if snapshot_id:
            query += " WHERE snapshot_id=?"
            params.append(snapshot_id)
        with self._connection() as connection:
            rows = connection.execute(
                query + " ORDER BY created_at DESC,id LIMIT ?", (*params, min(max(limit, 1), 500))
            ).fetchall()
        return {"merges": [self._merge_row(row) for row in rows]}

    @staticmethod
    def _hard_constraints(
        connection: sqlite3.Connection, source_ids: list[str], target_term_id: str,
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for source_id in source_ids:
            rows = connection.execute(
                "SELECT constraints_json FROM term_pair_judgment WHERE "
                "(source_term_id=? AND target_term_id=?) OR (source_term_id=? AND target_term_id=?)",
                (source_id, target_term_id, target_term_id, source_id),
            ).fetchall()
            for row in rows:
                found.extend(
                    item for item in json.loads(row["constraints_json"] or "[]")
                    if item.get("kind") == "hard"
                )
        return found

    # ── Semantic definitions ──

    def save_definition(self, snapshot_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        definition_id = "def-" + uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO term_definition
                   (id,snapshot_id,term_id,canonical_name,definition,facts_json,inferences_json,
                    unknowns_json,evidence_ids_json,confidence,grounding_json,model_name,prompt_version,
                    analysis_version,input_hash,status,created_at,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (definition_id, snapshot_id, definition.get("term_id", ""),
                 definition.get("canonical_name", ""), definition.get("definition", ""),
                 json.dumps(definition.get("facts", []), ensure_ascii=False),
                 json.dumps(definition.get("inferences", []), ensure_ascii=False),
                 json.dumps(definition.get("unknowns", []), ensure_ascii=False),
                 json.dumps(definition.get("evidence_ids", []), ensure_ascii=False),
                 definition.get("confidence", 0),
                 json.dumps(definition.get("grounding", {}), ensure_ascii=False),
                 definition.get("model_name", ""), definition.get("prompt_version", ""),
                 definition.get("analysis_version", ""), definition.get("input_hash", ""),
                 definition.get("status", "draft"), now,
                 json.dumps(definition, ensure_ascii=False)),
            )
            self._record_review(
                connection, snapshot_id=snapshot_id, item_type="DEFINITION",
                item_id=definition_id, item_version=1, action="generate",
                reason="grounding: " + ("passed" if (definition.get("grounding") or {}).get("passed") else "failed"),
                reviewer=definition.get("model_name", ""),
            )
        return self.get_definition(snapshot_id, definition.get("term_id", "")) or {}

    def get_definition(self, snapshot_id: str, term_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM term_definition WHERE snapshot_id=? AND term_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (snapshot_id, term_id),
            ).fetchone()
        return self._definition_row(row) if row else None

    def list_definitions(self, snapshot_id: str, status: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM term_definition WHERE snapshot_id=?"
        params: list[Any] = [snapshot_id]
        if status:
            query += " AND status=?"
            params.append(status)
        with self._connection() as connection:
            rows = connection.execute(
                query + " ORDER BY created_at DESC,rowid DESC", params
            ).fetchall()
        seen: set[str] = set()
        latest = []
        for row in rows:
            if row["term_id"] in seen:
                continue
            seen.add(row["term_id"])
            latest.append(self._definition_row(row))
        return latest

    def review_definition(
        self, definition_id: str, action: str, reason: str = "", reviewer: str = "",
        role: str = "",
    ) -> dict[str, Any]:
        if action not in {"accept", "reject"}:
            raise ValueError("unsupported definition review action")
        now = utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM term_definition WHERE id=?", (definition_id,)
            ).fetchone()
            if row is None:
                raise KeyError(definition_id)
            grounding = json.loads(row["grounding_json"] or "{}")
            if action == "accept" and not grounding.get("passed"):
                raise ValueError("grounding failed: definition can only remain a draft")
            connection.execute(
                "UPDATE term_definition SET status=? WHERE id=?",
                ("approved" if action == "accept" else "rejected", definition_id),
            )
            self._record_review(
                connection, snapshot_id=row["snapshot_id"], item_type="DEFINITION",
                item_id=definition_id, item_version=1, action=action,
                reason=reason, reviewer=reviewer, role=role,
            )
        return self._definition_row(self._fetch_one("term_definition", definition_id))

    # ── Governance: review history ──

    def list_review_records(
        self, snapshot_id: str = "", item_type: str = "", item_id: str = "", limit: int = 500,
    ) -> dict[str, Any]:
        clauses, params = [], []
        for column, value in (("snapshot_id", snapshot_id), ("item_type", item_type), ("item_id", item_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " AND ".join(clauses) if clauses else "1=1"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM review_record WHERE {where} ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (*params, min(max(limit, 1), 1000)),
            ).fetchall()
        return {"records": [dict(row) for row in rows]}

    # ── Immutable dictionary releases ──

    def create_release(
        self, snapshot_id: str, *, version: str, name: str = "", description: str = "",
        created_by: str = "", role: str = "", term_ids: list[str] | None = None,
        previous_release_id: str = "",
    ) -> dict[str, Any]:
        """Build an immutable release snapshot after gate checks."""
        version = version.strip()
        if not version:
            raise ValueError("version is required")
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM release_snapshot WHERE version=?", (version,)
            ).fetchone():
                raise ValueError("release version already exists")
            selected = self._release_candidates(connection, snapshot_id, term_ids)
            gates = self._release_gates(connection, snapshot_id, selected)
            if gates:
                raise ValueError("release gates failed: " + "; ".join(gates))
            term_items = []
            for term in selected:
                content = self._release_term_content(connection, snapshot_id, term)
                term_items.append(("TERM", term["id"], int(term.get("version") or 1), content))
            term_id_set = {item[1] for item in term_items}
            relations = connection.execute(
                f"""SELECT * FROM term_relations WHERE snapshot_id=?
                    AND source_term_id IN ({','.join('?' for _ in term_id_set)})
                    AND target_term_id IN ({','.join('?' for _ in term_id_set)})""",
                (snapshot_id, *term_id_set, *term_id_set),
            ).fetchall() if term_id_set else []
            relation_items = []
            for row in relations:
                if row["status"] != "accepted" and row["review_status"] != "accepted":
                    continue
                content = {key: row[key] for key in row.keys()}
                content["score_breakdown"] = json.loads(content.pop("score_breakdown_json", "{}"))
                content["evidence_refs"] = json.loads(content.pop("evidence_refs_json", "[]"))
                relation_items.append(("RELATION", row["id"], int(row["version"] or 1), content))
            items = term_items + relation_items
            if not items:
                raise ValueError("no publishable terms")
            release_id = "release-" + uuid4().hex
            item_hashes = []
            for item_type, item_id, item_version, content in items:
                content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
                item_hashes.append(sha256(content_json.encode("utf-8")).hexdigest())
                connection.execute(
                    """INSERT INTO release_item
                       (release_id,item_type,item_id,item_version,snapshot_content_json,content_hash)
                       VALUES(?,?,?,?,?,?)""",
                    (release_id, item_type, item_id, item_version, content_json, item_hashes[-1]),
                )
            snapshot_hash = sha256("|".join(sorted(item_hashes)).encode("utf-8")).hexdigest()
            connection.execute(
                """INSERT INTO release_snapshot
                   (release_id,release_name,version,status,created_by,description,snapshot_hash,
                    snapshot_id,previous_release_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (release_id, name or version, version, "published", created_by,
                 description, snapshot_hash, snapshot_id, previous_release_id, utc_now()),
            )
            self._record_review(
                connection, snapshot_id=snapshot_id, release_id=release_id,
                item_type="RELEASE", item_id=release_id, item_version=1,
                action="publish", reason=description, reviewer=created_by, role=role,
            )
        return self.get_release(release_id) or {}

    def list_releases(self, limit: int = 200) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM release_snapshot ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
        releases = []
        for row in rows:
            data = dict(row)
            with self._connection() as connection:
                data["item_count"] = connection.execute(
                    "SELECT COUNT(*) FROM release_item WHERE release_id=?", (row["release_id"],)
                ).fetchone()[0]
            releases.append(data)
        return {"releases": releases}

    def get_release(self, release_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM release_snapshot WHERE release_id=?", (release_id,)
            ).fetchone()
            if row is None:
                return None
            items = connection.execute(
                "SELECT * FROM release_item WHERE release_id=? ORDER BY item_type,item_id",
                (release_id,),
            ).fetchall()
        return {
            **dict(row),
            "items": [{
                "item_type": item["item_type"], "item_id": item["item_id"],
                "item_version": item["item_version"],
                "content": json.loads(item["snapshot_content_json"]),
                "content_hash": item["content_hash"],
            } for item in items],
        }

    def release_diff(self, release_id: str, compare_to: str = "") -> dict[str, Any]:
        release = self.get_release(release_id)
        if release is None:
            raise KeyError(release_id)
        current = {
            item["item_id"]: item for item in release["items"]
            if compare_to or item["item_type"] == "TERM"
        }
        if compare_to:
            other = self.get_release(compare_to)
            if other is None:
                raise KeyError(compare_to)
            baseline = {item["item_id"]: item for item in other["items"]}
            baseline_label = compare_to
        else:
            baseline, baseline_label = self._live_release_baseline(release), "live"
        added = sorted(set(baseline) - set(current))
        removed = sorted(set(current) - set(baseline))
        changed = sorted(
            item_id for item_id in set(current) & set(baseline)
            if current[item_id]["content_hash"] != baseline[item_id]["content_hash"]
        )
        return {
            "release_id": release_id, "compare_to": baseline_label,
            "added": [baseline[item] for item in added],
            "removed": [current[item] for item in removed],
            "changed": [{
                "release_item": current[item],
                "baseline_item": baseline[item],
                "fields": _changed_fields(current[item]["content"], baseline[item]["content"]),
            } for item in changed],
        }

    def _live_release_baseline(self, release: dict[str, Any]) -> dict[str, dict[str, Any]]:
        snapshot_id = str(release.get("snapshot_id") or "")
        if not snapshot_id:
            return {}
        with self._connection() as connection:
            terms = [dict(row) for row in connection.execute(
                "SELECT t.*,r.source_refs_json,r.term_confidence,r.aliases_json "
                "FROM terms t JOIN term_snapshot_results r ON r.term_id=t.id "
                "WHERE r.snapshot_id=? AND r.status='accepted' ORDER BY t.id",
                (snapshot_id,),
            ).fetchall()]
            baseline = {}
            for term in terms:
                content = self._release_term_content(connection, snapshot_id, term)
                baseline[term["id"]] = {
                    "item_type": "TERM", "item_id": term["id"],
                    "item_version": int(term.get("version") or 1),
                    "content": content,
                    "content_hash": sha256(
                        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                }
        return baseline

    def _release_candidates(
        self, connection: sqlite3.Connection, snapshot_id: str, term_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT t.*,r.status AS snapshot_status,r.lifecycle_status AS snapshot_lifecycle,"
            "r.source_refs_json,r.term_confidence,r.aliases_json FROM terms t JOIN term_snapshot_results r "
            "ON r.term_id=t.id WHERE r.snapshot_id=?"
        )
        params: list[Any] = [snapshot_id]
        if term_ids:
            query += f" AND t.id IN ({','.join('?' for _ in term_ids)})"
            params.extend(term_ids)
        rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        selected = [
            row for row in rows
            if row.get("snapshot_status") == "accepted"
            and row.get("lifecycle_status") not in {"MERGED", "REJECTED", "DEPRECATED"}
            and row.get("snapshot_lifecycle") not in {"MERGED", "REJECTED", "DEPRECATED"}
        ]
        if term_ids:
            rejected = sorted(set(term_ids) - {row["id"] for row in selected})
            if rejected:
                raise ValueError("terms are not publishable: " + ", ".join(rejected))
        return selected

    @staticmethod
    def _release_gates(
        connection: sqlite3.Connection, snapshot_id: str, terms: list[dict[str, Any]]
    ) -> list[str]:
        from ..analysis.knowledge.definitions import SENSITIVE_PATTERN

        gates: list[str] = []
        for term in terms:
            term_id = term["id"]
            conflicts = connection.execute(
                "SELECT COUNT(*) FROM evidence_conflict WHERE snapshot_id=? AND term_id=? AND resolved=0",
                (snapshot_id, term_id),
            ).fetchone()[0]
            if conflicts:
                gates.append(f"{term_id}: unresolved conflicts")
            if not (term.get("bounded_context") or "").strip():
                gates.append(f"{term_id}: bounded context is missing")
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM term_evidence WHERE snapshot_id=? AND term_id=?",
                (snapshot_id, term_id),
            ).fetchone()[0]
            if not evidence_count:
                gates.append(f"{term_id}: no evidence")
            definition = connection.execute(
                "SELECT grounding_json,payload_json FROM term_definition WHERE snapshot_id=? AND term_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (snapshot_id, term_id),
            ).fetchone()
            if definition is not None:
                if not json.loads(definition["grounding_json"] or "{}").get("passed"):
                    gates.append(f"{term_id}: definition grounding failed")
                if SENSITIVE_PATTERN.search(json.loads(definition["payload_json"] or "{}").get("definition", "")):
                    gates.append(f"{term_id}: definition contains sensitive data")
        return gates

    @staticmethod
    def _release_term_content(
        connection: sqlite3.Connection, snapshot_id: str, term: dict[str, Any]
    ) -> dict[str, Any]:
        evidence = connection.execute(
            "SELECT id,evidence_type,evidence_value,content_hash,source_version "
            "FROM term_evidence WHERE snapshot_id=? AND term_id=? ORDER BY id",
            (snapshot_id, term["id"]),
        ).fetchall()
        definition = connection.execute(
            "SELECT id,definition,status,prompt_version FROM term_definition "
            "WHERE snapshot_id=? AND term_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (snapshot_id, term["id"]),
        ).fetchone()
        return {
            "term_id": term["id"], "canonical_name": term["canonical_name"],
            "display_name": term.get("display_name") or term["canonical_name"],
            "term_type": term["term_type"], "bounded_context": term["bounded_context"],
            "source_of_truth": term.get("source_of_truth", ""),
            "term_confidence": term.get("term_confidence", 0),
            "definition": (definition["definition"] if definition else term.get("definition", "")),
            "definition_id": definition["id"] if definition else "",
            "definition_status": definition["status"] if definition else "",
            "aliases": json.loads(term.get("aliases_json") or "[]")
            if isinstance(term.get("aliases_json"), str) else term.get("aliases", []),
            "evidence": [dict(row) for row in evidence],
            "source_refs": json.loads(term.get("source_refs_json") or "[]"),
            "version": int(term.get("version") or 1),
        }

    # ── Incremental analysis runs (idempotent) ──

    def create_or_reuse_analysis_run(
        self, *, repository_id: str, commit_id: str, analysis_version: str,
        input_hash: str, scope: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_run WHERE repository_id=? AND commit_id=? "
                "AND analysis_version=? AND input_hash=?",
                (repository_id, commit_id, analysis_version, input_hash),
            ).fetchone()
            if row is not None and row["status"] == "completed":
                return dict(row), True
            if row is not None:
                connection.execute(
                    "UPDATE analysis_run SET status='running',started_at=?,finished_at=NULL,error_message='' WHERE id=?",
                    (now, row["id"]),
                )
                run_id = row["id"]
            else:
                run_id = "run-" + uuid4().hex
                connection.execute(
                    """INSERT INTO analysis_run
                       (id,repository_id,commit_id,analysis_version,input_hash,status,scope_json,started_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (run_id, repository_id, commit_id, analysis_version, input_hash,
                     "running", json.dumps(scope or {}, ensure_ascii=False), now),
                )
            current = connection.execute(
                "SELECT * FROM analysis_run WHERE id=?", (run_id,)
            ).fetchone()
        return dict(current), False

    def finish_analysis_run(
        self, run_id: str, *, status: str, result: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> dict[str, Any]:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE analysis_run SET status=?,result_json=?,error_message=?,finished_at=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), error_message, utc_now(), run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(run_id)
        return self.get_analysis_run(run_id) or {}

    def get_analysis_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._fetch_one("analysis_run", run_id)
        if row is None:
            return None
        data = dict(row)
        data["scope"] = json.loads(data.pop("scope_json", "{}"))
        data["result"] = json.loads(data.pop("result_json", "{}"))
        return data

    # ── Row conversion helpers ──

    def _fetch_one(self, table: str, row_id: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                f"SELECT * FROM {table} WHERE id=?", (row_id,)
            ).fetchone()

    @staticmethod
    def _pair_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["support_evidence"] = json.loads(data.pop("support_evidence_json", "[]"))
        data["against_evidence"] = json.loads(data.pop("against_evidence_json", "[]"))
        data["constraints"] = json.loads(data.pop("constraints_json", "[]"))
        data["merge_allowed"] = data.get("status") != "rejected" and not any(
            item.get("kind") == "hard" for item in data["constraints"]
        ) and data.get("judgment") in {"SAME_CONCEPT", "ALIAS"}
        return data

    @staticmethod
    def _merge_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["source_term_ids"] = json.loads(data.pop("source_term_ids_json", "[]"))
        data["evidence_refs"] = json.loads(data.pop("evidence_refs_json", "[]"))
        data["affected_relations"] = json.loads(data.pop("affected_relations_json", "{}"))
        return data

    @staticmethod
    def _definition_row(row: sqlite3.Row) -> dict[str, Any]:
        data = json.loads(row["payload_json"] or "{}") or dict(row)
        data.update({
            "id": row["id"], "snapshot_id": row["snapshot_id"], "term_id": row["term_id"],
            "status": row["status"], "confidence": row["confidence"],
            "grounding": json.loads(row["grounding_json"] or "{}"),
            "created_at": row["created_at"],
        })
        return data

    @staticmethod
    def _term_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["risk_flags"] = json.loads(data.pop("risk_flags_json", "[]"))
        data["aliases"] = json.loads(data.pop("aliases_json", "[]"))
        data["source_refs"] = json.loads(data.pop("source_refs_json", "[]") or "[]")
        data["term_type"] = canonical_term_type(data.get("term_type", ""))
        data["display_name"] = data.get("display_name") or data.get("canonical_name", "")
        data["domain_id"] = data.get("domain_id") or data.get("repository_id", "")
        data["term_confidence"] = data.get("term_confidence") or data.get("confidence_score", 0)
        data["lifecycle_status"] = data.get("lifecycle_status") or (
            "REJECTED" if data.get("status") == "rejected" else "DISCOVERED"
        )
        return data

    @staticmethod
    def _alignment_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["score_breakdown"] = json.loads(data.pop("score_breakdown_json", "{}"))
        data["service_relation_reasons"] = json.loads(data.pop("service_relation_reasons_json", "[]"))
        return data


def _changed_fields(current: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level diff between a release item and its live/previous value."""
    fields = []
    for key in sorted(set(current) | set(baseline)):
        if current.get(key) != baseline.get(key):
            fields.append({
                "field": key,
                "current": _truncate(current.get(key)),
                "baseline": _truncate(baseline.get(key)),
            })
    return fields


def _truncate(value: Any, limit: int = 300) -> Any:
    if isinstance(value, (list, dict)):
        return value if len(json.dumps(value, ensure_ascii=False)) <= limit else f"<{type(value).__name__} {len(value)}>"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value
