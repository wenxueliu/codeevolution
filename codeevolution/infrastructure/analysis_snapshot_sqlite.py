"""SQLite catalog and durable run/attempt state for repository snapshots.

The store deliberately opens a fresh configured connection per operation so a
scheduler worker never shares a SQLite connection with another worker.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import uuid4

from codeevolution.domain.analysis_snapshot import (
    TERMINAL_ATTEMPT_STATUSES,
    AnalysisRun,
    AnalysisRunMember,
    AnalysisScope,
    AttemptStage,
    AttemptStatus,
    EvidenceBundle,
    GraphView,
    GraphViewMember,
    RepositoryAnalysisSnapshot,
    RepositoryAttempt,
    RepositoryMember,
    RunMemberDisposition,
    RunStatus,
    ViewAvailability,
    ViewLifecycle,
    aggregate_run_status,
)
from codeevolution.domain.topology import canonical_aliases, canonical_digest
from codeevolution.platform import ensure_supported_storage_path, set_private_permissions

SCHEMA_VERSION = 6
DEFAULT_TOPOLOGY_RULES_DIGEST = canonical_digest({"schema": "topology-rules/v1", "rules": []})

# Retention is deliberately expressed in one place so an administrator can
# use the same defaults for the catalog scavenger and the CAS sweeper.  The
# values mirror the contract in the topology implementation design.
DEFAULT_TOPOLOGY_CACHE_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_TOPOLOGY_JOB_RETENTION_SECONDS = 90 * 24 * 60 * 60
DEFAULT_TOPOLOGY_READER_LEASE_SECONDS = 60
MAX_ARTIFACT_REQUEST_SPEC_BYTES = 64 * 1024
MAX_ARTIFACT_ERROR_BYTES = 2_000

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS snapshot_rule_candidates (
    id TEXT PRIMARY KEY,
    rule_kind TEXT NOT NULL CHECK(rule_kind IN ('node','business')),
    repository_snapshot_id TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    view_id TEXT,
    subject_key TEXT NOT NULL,
    prompt TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
    result TEXT, error TEXT, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_rule_current (
    rule_kind TEXT NOT NULL, repository_snapshot_id TEXT NOT NULL,
    view_id TEXT NOT NULL DEFAULT '', subject_key TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES snapshot_rule_candidates(id) ON DELETE RESTRICT,
    PRIMARY KEY(rule_kind,repository_snapshot_id,view_id,subject_key)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_rule_candidates
ON snapshot_rule_candidates(repository_snapshot_id,rule_kind,subject_key,created_at DESC);
CREATE TABLE IF NOT EXISTS graph_view_artifact_jobs (
    id TEXT PRIMARY KEY, view_id TEXT REFERENCES graph_views(id) ON DELETE SET NULL,
    cache_key_digest TEXT NOT NULL, artifact_kind TEXT NOT NULL, status TEXT NOT NULL
      CHECK(status IN ('pending','running','completed','failed','cancelled')),
    payload_json TEXT, error_message TEXT, requested_at TEXT NOT NULL, completed_at TEXT,
    UNIQUE(cache_key_digest)
);
CREATE INDEX IF NOT EXISTS idx_graph_artifact_jobs_view ON graph_view_artifact_jobs(view_id,status);
CREATE TABLE IF NOT EXISTS deletion_jobs (
    id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','trashed','completed','failed')),
    requested_at TEXT NOT NULL, completed_at TEXT, trash_path TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS storage_reservations (
    attempt_id TEXT PRIMARY KEY REFERENCES repository_attempts(id) ON DELETE CASCADE,
    reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
"""

_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS graph_view_artifact_jobs (
    id TEXT PRIMARY KEY, view_id TEXT REFERENCES graph_views(id) ON DELETE SET NULL,
    cache_key_digest TEXT NOT NULL, artifact_kind TEXT NOT NULL, status TEXT NOT NULL,
    payload_json TEXT, error_message TEXT, requested_at TEXT NOT NULL, completed_at TEXT,
    UNIQUE(cache_key_digest)
);
CREATE TABLE IF NOT EXISTS graph_view_artifact_cache (
    cache_key_digest TEXT PRIMARY KEY, view_digest TEXT NOT NULL,
    artifact_kind TEXT NOT NULL, params_digest TEXT NOT NULL DEFAULT '',
    analyzer_bundle_digest TEXT NOT NULL DEFAULT '', rules_digest TEXT NOT NULL DEFAULT '',
    artifact_schema_version TEXT NOT NULL DEFAULT '1', semantic_identity_digest TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, last_accessed_at TEXT NOT NULL,
    payload_storage TEXT NOT NULL DEFAULT 'inline' CHECK(payload_storage IN ('inline','artifact')),
    payload_json TEXT, artifact_key TEXT, payload_digest TEXT NOT NULL DEFAULT '', byte_size INTEGER NOT NULL DEFAULT 0,
    UNIQUE(view_digest, artifact_kind, params_digest, analyzer_bundle_digest, rules_digest, artifact_schema_version, semantic_identity_digest),
    CHECK((payload_storage='inline' AND payload_json IS NOT NULL AND artifact_key IS NULL) OR
          (payload_storage='artifact' AND payload_json IS NULL AND artifact_key IS NOT NULL))
);
CREATE TABLE graph_view_artifact_jobs_v3 (
    id TEXT PRIMARY KEY, view_id TEXT REFERENCES graph_views(id) ON DELETE SET NULL,
    cache_key_digest TEXT NOT NULL, artifact_kind TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 1,
    retry_of_job_id TEXT REFERENCES graph_view_artifact_jobs_v3(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled','interrupted')),
    payload_json TEXT, error_code TEXT, error_message TEXT, requested_at TEXT NOT NULL,
    started_at TEXT, completed_at TEXT,
    request_spec_json TEXT NOT NULL DEFAULT '{}', request_spec_digest TEXT NOT NULL DEFAULT '',
    worker_id TEXT, lease_token TEXT, lease_until TEXT, heartbeat_at TEXT,
    stage TEXT NOT NULL DEFAULT 'queued', progress_json TEXT NOT NULL DEFAULT '{}',
    cancellation_requested_at TEXT, UNIQUE(cache_key_digest, attempt_no)
);
INSERT INTO graph_view_artifact_jobs_v3
 (id,view_id,cache_key_digest,artifact_kind,attempt_no,status,payload_json,error_message,requested_at,completed_at)
 SELECT id,view_id,cache_key_digest,artifact_kind,1,status,payload_json,error_message,requested_at,completed_at
 FROM graph_view_artifact_jobs;
DROP TABLE graph_view_artifact_jobs;
ALTER TABLE graph_view_artifact_jobs_v3 RENAME TO graph_view_artifact_jobs;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_graph_artifact_job
 ON graph_view_artifact_jobs(cache_key_digest) WHERE status IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_graph_artifact_jobs_view ON graph_view_artifact_jobs(view_id,status);
"""

_MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS llm_knowledge_jobs (
    id TEXT PRIMARY KEY,
    repository_snapshot_id TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    progress_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_message TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_knowledge_jobs_snapshot
    ON llm_knowledge_jobs(repository_snapshot_id, requested_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_llm_knowledge_job
    ON llm_knowledge_jobs(repository_snapshot_id)
    WHERE status IN ('pending','running');
"""

_REPAIR_DDL = """
CREATE TABLE IF NOT EXISTS deletion_jobs (
    id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','trashed','completed','failed')),
    requested_at TEXT NOT NULL, completed_at TEXT, trash_path TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS storage_reservations (
    attempt_id TEXT PRIMARY KEY REFERENCES repository_attempts(id) ON DELETE CASCADE,
    reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_view_artifact_readers (
    cache_key_digest TEXT NOT NULL,
    reader_id TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    PRIMARY KEY(cache_key_digest, reader_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_artifact_readers_expiry
    ON graph_view_artifact_readers(lease_until);
"""

_ARTIFACT_JOB_V3_TABLE = """
CREATE TABLE graph_view_artifact_jobs_v3 (
    id TEXT PRIMARY KEY, view_id TEXT REFERENCES graph_views(id) ON DELETE SET NULL,
    cache_key_digest TEXT NOT NULL, artifact_kind TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 1,
    retry_of_job_id TEXT REFERENCES graph_view_artifact_jobs_v3(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled','interrupted')),
    payload_json TEXT, error_code TEXT, error_message TEXT, requested_at TEXT NOT NULL,
    started_at TEXT, completed_at TEXT, UNIQUE(cache_key_digest, attempt_no)
);
"""

_MIGRATION_1 = """
CREATE TABLE analysis_scopes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, retired_at TEXT
);
CREATE TABLE repository_members (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES analysis_scopes(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL, registered_path TEXT NOT NULL, path_identity TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retired_at TEXT,
    declared_aliases_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(scope_id, display_name)
);
CREATE INDEX idx_members_scope_active ON repository_members(scope_id, retired_at);
CREATE UNIQUE INDEX uq_active_member_path ON repository_members(path_identity)
    WHERE retired_at IS NULL;

CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN
        ('pending','running','completed','partial','failed','cancelled','interrupted')),
    requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
    external_context TEXT NOT NULL DEFAULT '{}', tags_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT,
    CHECK(length(external_context) <= 16384), CHECK(length(tags_json) <= 4096)
);
CREATE UNIQUE INDEX uq_runs_idempotency ON analysis_runs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE evidence_bundles (
    digest TEXT PRIMARY KEY, artifact_key TEXT NOT NULL UNIQUE,
    manifest_schema_version TEXT NOT NULL, source_digest TEXT NOT NULL,
    graph_digest TEXT NOT NULL, graph_blob_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0), capture_policy_digest TEXT NOT NULL,
    capture_completeness TEXT NOT NULL CHECK(capture_completeness IN ('complete','incomplete')),
    created_at TEXT NOT NULL,
    deletion_state TEXT NOT NULL DEFAULT 'active'
      CHECK(deletion_state IN ('active','deletion_pending','trashed'))
);
CREATE TABLE repository_analysis_snapshots (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    evidence_digest TEXT NOT NULL REFERENCES evidence_bundles(digest) ON DELETE RESTRICT,
    captured_at TEXT NOT NULL, observed_branch TEXT, observed_head_commit TEXT,
    dirty INTEGER NOT NULL CHECK(dirty IN (0,1)), codegraph_version TEXT NOT NULL,
    codegraph_schema_digest TEXT NOT NULL, analyzer_bundle_digest TEXT NOT NULL,
    rules_digest TEXT NOT NULL, report_schema_version TEXT NOT NULL, options_digest TEXT NOT NULL,
    facts_digest TEXT NOT NULL, facts_storage TEXT NOT NULL CHECK(facts_storage IN ('inline','artifact')),
    facts_json TEXT, facts_artifact_key TEXT,
    analysis_completeness TEXT NOT NULL CHECK(analysis_completeness IN ('complete','incomplete')),
    orphaned INTEGER NOT NULL DEFAULT 0 CHECK(orphaned IN (0,1)),
    deletion_state TEXT NOT NULL DEFAULT 'active'
      CHECK(deletion_state IN ('active','deletion_pending','trashed')),
    CHECK((facts_storage='inline' AND facts_json IS NOT NULL AND facts_artifact_key IS NULL)
       OR (facts_storage='artifact' AND facts_json IS NULL AND facts_artifact_key IS NOT NULL)),
    UNIQUE(member_id, evidence_digest, analyzer_bundle_digest, rules_digest,
           report_schema_version, options_digest)
);
CREATE TABLE repository_snapshot_metadata (
    snapshot_id TEXT PRIMARY KEY REFERENCES repository_analysis_snapshots(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
    metadata_version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE current_repository_snapshots (
    member_id TEXT PRIMARY KEY REFERENCES repository_members(id) ON DELETE RESTRICT,
    snapshot_id TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    published_at TEXT NOT NULL
);
CREATE TRIGGER trg_current_snapshot_member_insert BEFORE INSERT ON current_repository_snapshots
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM repository_analysis_snapshots s JOIN evidence_bundles e ON e.digest=s.evidence_digest
    WHERE s.id=NEW.snapshot_id AND s.member_id=NEW.member_id AND s.orphaned=0
      AND s.deletion_state='active' AND e.deletion_state='active'
  ) THEN RAISE(ABORT,'current_snapshot_member_mismatch') END;
END;
CREATE TRIGGER trg_current_snapshot_member_update BEFORE UPDATE ON current_repository_snapshots
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM repository_analysis_snapshots s JOIN evidence_bundles e ON e.digest=s.evidence_digest
    WHERE s.id=NEW.snapshot_id AND s.member_id=NEW.member_id AND s.orphaned=0
      AND s.deletion_state='active' AND e.deletion_state='active'
  ) THEN RAISE(ABORT,'current_snapshot_member_mismatch') END;
END;
CREATE TRIGGER trg_snapshot_cannot_invalidate_current
BEFORE UPDATE OF deletion_state,orphaned ON repository_analysis_snapshots
WHEN (NEW.deletion_state!='active' OR NEW.orphaned!=0)
 AND EXISTS (SELECT 1 FROM current_repository_snapshots c WHERE c.snapshot_id=NEW.id)
BEGIN SELECT RAISE(ABORT,'current_snapshot_cannot_be_invalidated'); END;
CREATE TRIGGER trg_evidence_cannot_invalidate_current
BEFORE UPDATE OF deletion_state ON evidence_bundles
WHEN NEW.deletion_state!='active' AND EXISTS (
 SELECT 1 FROM repository_analysis_snapshots s JOIN current_repository_snapshots c ON c.snapshot_id=s.id
 WHERE s.evidence_digest=NEW.digest)
BEGIN SELECT RAISE(ABORT,'current_evidence_cannot_be_invalidated'); END;

CREATE TABLE repository_attempts (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN
      ('pending','running','completed','unchanged','failed','cancelled','interrupted')),
    stage TEXT NOT NULL CHECK(stage IN
      ('queued','validating','digest_before','codegraph_init','codegraph_sync','digest_after',
       'freezing_graph','freezing_sources','digest_final','analyzing','publishing','finished')),
    requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
    retry_of_attempt_id TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    blocking_attempt_id TEXT REFERENCES repository_attempts(id) ON DELETE SET NULL,
    snapshot_id TEXT REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    cancellation_requested_at TEXT, worker_instance_id TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}', error_code TEXT, error_message TEXT,
    error_details_json TEXT NOT NULL DEFAULT '{}', log_excerpt TEXT,
    UNIQUE(member_id, attempt_no)
);
CREATE UNIQUE INDEX uq_active_attempt_per_member ON repository_attempts(member_id)
    WHERE status IN ('pending','running');
CREATE INDEX idx_attempts_run ON repository_attempts(run_id);
CREATE INDEX idx_attempts_member_time ON repository_attempts(member_id, requested_at DESC);

CREATE TABLE analysis_run_members (
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('queued','already_running')),
    attempt_id TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    blocking_attempt_id TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    PRIMARY KEY(run_id, member_id),
    CHECK((disposition='queued' AND attempt_id IS NOT NULL AND blocking_attempt_id IS NULL)
       OR (disposition='already_running' AND attempt_id IS NULL AND blocking_attempt_id IS NOT NULL))
);
CREATE TABLE attempt_observations (
    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES repository_attempts(id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK(phase IN ('before_sync','after_sync','after_freeze')),
    observed_at TEXT NOT NULL, input_digest TEXT NOT NULL, observed_branch TEXT,
    observed_head_commit TEXT, dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
    file_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL, UNIQUE(attempt_id, phase)
);
CREATE TABLE member_change_checks (
    member_id TEXT PRIMARY KEY REFERENCES repository_members(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN
      ('unchanged','source_changed','analyzer_outdated','unparsed','check_failed')),
    checked_at TEXT NOT NULL, input_digest TEXT, observed_branch TEXT,
    observed_head_commit TEXT, dirty INTEGER CHECK(dirty IN (0,1)),
    error_code TEXT, error_message TEXT
);

CREATE TABLE graph_views (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL,
    scope_id TEXT NOT NULL REFERENCES analysis_scopes(id) ON DELETE RESTRICT,
    identity_schema TEXT NOT NULL DEFAULT 'graph-view/v2',
    topology_rules_digest TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ephemeral','pinned')),
    completeness TEXT NOT NULL CHECK(completeness IN ('complete','incomplete')),
    created_at TEXT NOT NULL, last_accessed_at TEXT NOT NULL, expires_at TEXT,
    selector_json TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    external_context TEXT NOT NULL DEFAULT '{}', tags_json TEXT NOT NULL DEFAULT '[]',
    CHECK((lifecycle='ephemeral' AND expires_at IS NOT NULL)
       OR (lifecycle='pinned' AND expires_at IS NULL)),
    CHECK(length(external_context)<=16384), CHECK(length(tags_json)<=4096)
);
CREATE INDEX idx_views_digest ON graph_views(digest);
CREATE INDEX idx_views_expiry ON graph_views(lifecycle,expires_at);
CREATE TABLE graph_view_members (
    view_id TEXT NOT NULL REFERENCES graph_views(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    member_id TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    snapshot_id TEXT REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    availability TEXT NOT NULL CHECK(availability IN ('available','unparsed','retired')),
    display_name TEXT NOT NULL,
    declared_aliases_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(view_id,member_id), UNIQUE(view_id,ordinal),
    CHECK((availability='available' AND snapshot_id IS NOT NULL)
       OR (availability!='available' AND snapshot_id IS NULL))
);
CREATE TRIGGER trg_view_snapshot_member_insert BEFORE INSERT ON graph_view_members
WHEN NEW.snapshot_id IS NOT NULL
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM repository_analysis_snapshots s JOIN evidence_bundles e ON e.digest=s.evidence_digest
    WHERE s.id=NEW.snapshot_id AND s.member_id=NEW.member_id
      AND s.deletion_state='active' AND e.deletion_state='active'
  ) THEN RAISE(ABORT,'snapshot_view_member_mismatch') END;
END;
CREATE TRIGGER trg_view_member_scope_insert BEFORE INSERT ON graph_view_members
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM graph_views v JOIN repository_members m ON m.id=NEW.member_id
    WHERE v.id=NEW.view_id AND v.scope_id=m.scope_id
  ) THEN RAISE(ABORT,'graph_view_member_scope_mismatch') END;
END;
CREATE TABLE snapshot_references (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    owner_type TEXT NOT NULL CHECK(owner_type IN
      ('current','ephemeral_view','pinned_view','snapshot_pin','node_rule','business_rule',
       'api_explanation','artifact_job')),
    owner_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('temporary','permanent')),
    created_at TEXT NOT NULL, expires_at TEXT,
    UNIQUE(snapshot_id,owner_type,owner_id)
);
CREATE INDEX idx_snapshot_refs_snapshot ON snapshot_references(snapshot_id,state,expires_at);
"""


class SnapshotStoreError(ValueError):
    """Base error for invalid catalog or lifecycle operations."""


class AttemptStateConflictError(SnapshotStoreError):
    """Raised when an attempt compare-and-swap loses or a transition is invalid."""


class ViewExpiredError(SnapshotStoreError):
    """Raised when a caller tries to use an expired ephemeral view."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AnalysisSnapshotSQLiteStore:
    """Persistence adapter for catalog and analysis execution state."""

    def __init__(self, db_path: str | Path):
        ensure_supported_storage_path(Path(db_path).parent)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private_permissions(self.db_path.parent, directory=True)
        self.migrate()
        if self.db_path.exists():
            set_private_permissions(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _repair_artifact_job_shape(connection: sqlite3.Connection) -> None:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_views'"
        ).fetchone() is None:
            return
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_view_artifact_jobs'"
        ).fetchone()
        if row is None:
            connection.executescript(_ARTIFACT_JOB_V3_TABLE.replace("graph_view_artifact_jobs_v3", "graph_view_artifact_jobs"))
            return
        columns = {item["name"] for item in connection.execute("PRAGMA table_info(graph_view_artifact_jobs)")}
        if "attempt_no" in columns:
            additions = {
                "request_spec_json": "TEXT NOT NULL DEFAULT '{}'",
                "request_spec_digest": "TEXT NOT NULL DEFAULT ''",
                "worker_id": "TEXT",
                "lease_token": "TEXT",
                "lease_until": "TEXT",
                "heartbeat_at": "TEXT",
                "stage": "TEXT NOT NULL DEFAULT 'queued'",
                "progress_json": "TEXT NOT NULL DEFAULT '{}'",
                "cancellation_requested_at": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE graph_view_artifact_jobs ADD COLUMN {name} {definition}")
            return
        connection.execute("ALTER TABLE graph_view_artifact_jobs RENAME TO graph_view_artifact_jobs_legacy")
        connection.executescript(_ARTIFACT_JOB_V3_TABLE)
        connection.execute(
            """INSERT INTO graph_view_artifact_jobs_v3
               (id,view_id,cache_key_digest,artifact_kind,attempt_no,status,payload_json,error_message,requested_at,completed_at)
               SELECT id,view_id,cache_key_digest,artifact_kind,1,status,payload_json,error_message,requested_at,completed_at
               FROM graph_view_artifact_jobs_legacy"""
        )
        connection.execute("DROP TABLE graph_view_artifact_jobs_legacy")
        connection.execute("ALTER TABLE graph_view_artifact_jobs_v3 RENAME TO graph_view_artifact_jobs")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_graph_artifact_job ON graph_view_artifact_jobs(cache_key_digest) WHERE status IN ('pending','running')"
        )

    @staticmethod
    def _repair_topology_contract_shape(connection: sqlite3.Connection) -> None:
        """Forward-migrate frozen View identity without guessing mixed scopes.

        SQLite cannot add a non-null column to populated tables without a
        default.  Old views therefore retain a null ``scope_id`` only when
        their historical members span scopes; consumers can report the
        documented ``legacy_mixed_scope_view`` error instead of silently
        choosing one.
        """
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_views'"
        ).fetchone() is None:
            return
        view_columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(graph_views)")
        }
        if "scope_id" not in view_columns:
            connection.execute("ALTER TABLE graph_views ADD COLUMN scope_id TEXT")
        if "identity_schema" not in view_columns:
            connection.execute(
                "ALTER TABLE graph_views ADD COLUMN identity_schema TEXT NOT NULL DEFAULT 'graph-view/v2'"
            )
        if "topology_rules_digest" not in view_columns:
            connection.execute("ALTER TABLE graph_views ADD COLUMN topology_rules_digest TEXT NOT NULL DEFAULT ''")

        member_columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(graph_view_members)")
        }
        if "display_name" not in member_columns:
            connection.execute("ALTER TABLE graph_view_members ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        if "declared_aliases_json" not in member_columns:
            connection.execute(
                "ALTER TABLE graph_view_members ADD COLUMN declared_aliases_json TEXT NOT NULL DEFAULT '[]'"
            )

        catalog_columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(repository_members)")
        }
        if "declared_aliases_json" not in catalog_columns:
            connection.execute(
                "ALTER TABLE repository_members ADD COLUMN declared_aliases_json TEXT NOT NULL DEFAULT '[]'"
            )

        connection.execute(
            """UPDATE graph_views SET scope_id=(
                   SELECT MIN(m.scope_id) FROM graph_view_members vm
                   JOIN repository_members m ON m.id=vm.member_id
                   WHERE vm.view_id=graph_views.id
                 )
                 WHERE scope_id IS NULL AND 1=(
                   SELECT COUNT(DISTINCT m.scope_id) FROM graph_view_members vm
                   JOIN repository_members m ON m.id=vm.member_id
                   WHERE vm.view_id=graph_views.id
                 )"""
        )
        connection.execute(
            "UPDATE graph_views SET topology_rules_digest=? WHERE topology_rules_digest=''",
            (DEFAULT_TOPOLOGY_RULES_DIGEST,),
        )
        connection.execute(
            """UPDATE graph_view_members
                   SET display_name=(SELECT display_name FROM repository_members m WHERE m.id=graph_view_members.member_id)
                 WHERE display_name=''"""
        )
        connection.execute(
            """UPDATE graph_view_members
                   SET declared_aliases_json=(SELECT declared_aliases_json FROM repository_members m WHERE m.id=graph_view_members.member_id)
                 WHERE declared_aliases_json='[]'"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_view_member_scope_insert
               BEFORE INSERT ON graph_view_members
               WHEN (SELECT scope_id FROM graph_views WHERE id=NEW.view_id) IS NOT NULL
               BEGIN
                 SELECT CASE WHEN NOT EXISTS (
                   SELECT 1 FROM graph_views v JOIN repository_members m ON m.id=NEW.member_id
                   WHERE v.id=NEW.view_id AND v.scope_id=m.scope_id
                 ) THEN RAISE(ABORT,'graph_view_member_scope_mismatch') END;
               END"""
        )

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # Older v2 databases created before the lifecycle tables landed may
            # already advertise the current user_version; repair those tables
            # idempotently before serving requests.
            connection.executescript(_REPAIR_DDL)
            self._repair_artifact_job_shape(connection)
            connection.commit()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"analysis snapshot schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version == 0:
                try:
                    connection.executescript(
                        f"BEGIN EXCLUSIVE;\n{_MIGRATION_1}\n{_MIGRATION_2}\n{_MIGRATION_3}\n"
                        f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                    )
                except BaseException:
                    connection.rollback()
                    raise
            elif version == 1:
                try:
                    connection.executescript(
                        f"BEGIN EXCLUSIVE;\n{_MIGRATION_2}\n{_MIGRATION_3}\n"
                        f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                    )
                except BaseException:
                    connection.rollback()
                    raise
            elif version == 2:
                try:
                    connection.executescript(
                        f"BEGIN EXCLUSIVE;\n{_MIGRATION_3}\n"
                        f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                    )
                except BaseException:
                    connection.rollback()
                    raise
            # The initial schema now includes v4 columns.  Existing v1-v3
            # databases get them through the idempotent repair below.
            self._repair_topology_contract_shape(connection)
            # Keep this migration idempotent so databases created by older
            # versions and fresh databases both get durable LLM job state.
            connection.executescript(_MIGRATION_4)
            if version < SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def create_scope(self, name: str, *, scope_id: str | None = None) -> AnalysisScope:
        if not name.strip():
            raise SnapshotStoreError("scope name must not be blank")
        item = AnalysisScope(scope_id or str(uuid4()), name.strip(), utc_now())
        with self.connection() as connection, connection:
            connection.execute(
                "INSERT INTO analysis_scopes(id,name,created_at) VALUES(?,?,?)",
                (item.id, item.name, item.created_at),
            )
        return item

    def get_scope(self, scope_id: str) -> AnalysisScope | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_scopes WHERE id=?", (scope_id,)
            ).fetchone()
        return self._scope(row) if row else None

    def list_scopes(self, *, include_retired: bool = False) -> list[AnalysisScope]:
        sql = "SELECT * FROM analysis_scopes"
        if not include_retired:
            sql += " WHERE retired_at IS NULL"
        sql += " ORDER BY created_at,id"
        with self.connection() as connection:
            rows = connection.execute(sql).fetchall()
        return [self._scope(row) for row in rows]

    def rename_scope(self, scope_id: str, name: str) -> AnalysisScope:
        if not name.strip():
            raise SnapshotStoreError("scope name must not be blank")
        with self.connection() as connection, connection:
            if connection.execute(
                "UPDATE analysis_scopes SET name=? WHERE id=?", (name.strip(), scope_id)
            ).rowcount != 1:
                raise KeyError(scope_id)
        return self.get_scope(scope_id)  # type: ignore[return-value]

    def retire_scope(self, scope_id: str) -> AnalysisScope:
        now = utc_now()
        with self.connection() as connection, connection:
            active = connection.execute(
                "SELECT 1 FROM repository_members WHERE scope_id=? AND retired_at IS NULL LIMIT 1",
                (scope_id,),
            ).fetchone()
            if active:
                raise SnapshotStoreError("a scope with active members cannot be retired")
            if connection.execute(
                "UPDATE analysis_scopes SET retired_at=? WHERE id=? AND retired_at IS NULL",
                (now, scope_id),
            ).rowcount != 1:
                raise KeyError(scope_id)
        return self.get_scope(scope_id)  # type: ignore[return-value]

    def create_member(
        self,
        scope_id: str,
        display_name: str,
        registered_path: str,
        path_identity: str,
        *,
        member_id: str | None = None,
        declared_aliases: Sequence[str] = (),
    ) -> RepositoryMember:
        if not display_name.strip() or not registered_path or not path_identity:
            raise SnapshotStoreError("member name, path and path identity are required")
        now = utc_now()
        aliases = canonical_aliases(declared_aliases)
        item = RepositoryMember(
            member_id or str(uuid4()), scope_id, display_name.strip(), registered_path,
            path_identity, now, now, declared_aliases=aliases,
        )
        with self.connection() as connection, connection:
            connection.execute(
                """INSERT INTO repository_members
                   (id,scope_id,display_name,registered_path,path_identity,created_at,updated_at,declared_aliases_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (item.id, item.scope_id, item.display_name, item.registered_path,
                 item.path_identity, item.created_at, item.updated_at, _canonical_json(list(item.declared_aliases))),
            )
        return item

    def get_member(self, member_id: str) -> RepositoryMember | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repository_members WHERE id=?", (member_id,)
            ).fetchone()
        return self._member(row) if row else None

    def list_members(
        self, scope_id: str | None = None, *, include_retired: bool = False
    ) -> list[RepositoryMember]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_id is not None:
            clauses.append("scope_id=?")
            params.append(scope_id)
        if not include_retired:
            clauses.append("retired_at IS NULL")
        sql = "SELECT * FROM repository_members"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at,id"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._member(row) for row in rows]

    def update_member(
        self,
        member_id: str,
        *,
        display_name: str | None = None,
        registered_path: str | None = None,
        path_identity: str | None = None,
    ) -> RepositoryMember:
        current = self.get_member(member_id)
        if current is None:
            raise KeyError(member_id)
        name = current.display_name if display_name is None else display_name.strip()
        path = current.registered_path if registered_path is None else registered_path
        identity = current.path_identity if path_identity is None else path_identity
        if not name or not path or not identity:
            raise SnapshotStoreError("member name, path and path identity are required")
        with self.connection() as connection, connection:
            connection.execute(
                """UPDATE repository_members SET display_name=?,registered_path=?,
                   path_identity=?,updated_at=? WHERE id=?""",
                (name, path, identity, utc_now(), member_id),
            )
        return self.get_member(member_id)  # type: ignore[return-value]

    def retire_member(self, member_id: str) -> RepositoryMember:
        now = utc_now()
        with self.connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE repository_members SET retired_at=?,updated_at=? WHERE id=? AND retired_at IS NULL",
                (now, now, member_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(member_id)
        return self.get_member(member_id)  # type: ignore[return-value]

    def create_run(
        self,
        member_ids: Sequence[str],
        *,
        external_context: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        idempotency_key: str | None = None,
        retry_of: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> AnalysisRun:
        if not member_ids or len(set(member_ids)) != len(member_ids):
            raise SnapshotStoreError("member_ids must be non-empty and unique")
        context = external_context or {}
        if not isinstance(context, dict):
            raise SnapshotStoreError("external_context must be an object")
        if len(tags) > 32 or len(set(tags)) != len(tags) or any(
            not isinstance(tag, str) or len(tag) > 64 for tag in tags
        ):
            raise SnapshotStoreError("tags must be unique strings (maximum 32, 64 characters each)")
        context_json, tags_json = _canonical_json(context), _canonical_json(list(tags))
        if len(context_json) > 16384 or len(tags_json) > 4096:
            raise SnapshotStoreError("run metadata is too large")
        now, new_run_id = utc_now(), run_id or str(uuid4())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = connection.execute(
                        "SELECT id FROM analysis_runs WHERE idempotency_key=?", (idempotency_key,)
                    ).fetchone()
                    if existing:
                        connection.rollback()
                        return self.get_run(existing["id"])  # type: ignore[return-value]
                placeholders = ",".join("?" for _ in member_ids)
                found = connection.execute(
                    f"SELECT id FROM repository_members WHERE retired_at IS NULL AND id IN ({placeholders})",
                    tuple(member_ids),
                ).fetchall()
                if {row["id"] for row in found} != set(member_ids):
                    raise SnapshotStoreError("all selected members must exist and be active")
                connection.execute(
                    """INSERT INTO analysis_runs
                       (id,status,requested_at,external_context,tags_json,idempotency_key)
                       VALUES(?,?,?,?,?,?)""",
                    (new_run_id, RunStatus.PENDING.value, now, context_json, tags_json,
                     idempotency_key),
                )
                for ordinal, member_id in enumerate(member_ids):
                    active = connection.execute(
                        """SELECT id FROM repository_attempts WHERE member_id=?
                           AND status IN ('pending','running')""",
                        (member_id,),
                    ).fetchone()
                    if active:
                        connection.execute(
                            """INSERT INTO analysis_run_members
                               (run_id,member_id,ordinal,disposition,blocking_attempt_id)
                               VALUES(?,?,?,?,?)""",
                            (new_run_id, member_id, ordinal,
                             RunMemberDisposition.ALREADY_RUNNING.value, active["id"]),
                        )
                        continue
                    attempt_no = connection.execute(
                        "SELECT COALESCE(MAX(attempt_no),0)+1 FROM repository_attempts WHERE member_id=?",
                        (member_id,),
                    ).fetchone()[0]
                    attempt_id = str(uuid4())
                    retry_id = (retry_of or {}).get(member_id)
                    connection.execute(
                        """INSERT INTO repository_attempts
                           (id,run_id,member_id,attempt_no,status,stage,requested_at,retry_of_attempt_id)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (attempt_id, new_run_id, member_id, attempt_no,
                         AttemptStatus.PENDING.value, AttemptStage.QUEUED.value, now, retry_id),
                    )
                    connection.execute(
                        """INSERT INTO analysis_run_members
                           (run_id,member_id,ordinal,disposition,attempt_id) VALUES(?,?,?,?,?)""",
                        (new_run_id, member_id, ordinal,
                         RunMemberDisposition.QUEUED.value, attempt_id),
                    )
                self._recompute_run(connection, new_run_id, now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_run(new_run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> AnalysisRun | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            members = connection.execute(
                "SELECT * FROM analysis_run_members WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return self._run(row, members)

    def get_attempt(self, attempt_id: str) -> RepositoryAttempt | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repository_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return self._attempt(row) if row else None

    def list_attempts(self, run_id: str) -> list[RepositoryAttempt]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM repository_attempts WHERE run_id=? ORDER BY requested_at,id", (run_id,)
            ).fetchall()
        return [self._attempt(row) for row in rows]

    def active_attempt_ids(self) -> set[str]:
        """Return attempt IDs whose workers may still own staging trees."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM repository_attempts WHERE status IN ('pending','running')"
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def record_observation(
        self,
        attempt_id: str,
        phase: str,
        *,
        input_digest: str,
        observed_branch: str | None,
        observed_head_commit: str | None,
        dirty: bool,
        file_count: int,
        total_bytes: int,
    ) -> None:
        if phase not in {"before_sync", "after_sync", "after_freeze"}:
            raise SnapshotStoreError("unknown observation phase")
        with self.connection() as connection, connection:
            connection.execute(
                """INSERT INTO attempt_observations
                   (id,attempt_id,phase,observed_at,input_digest,observed_branch,
                    observed_head_commit,dirty,file_count,total_bytes)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), attempt_id, phase, utc_now(), input_digest,
                    observed_branch, observed_head_commit, int(dirty), file_count, total_bytes,
                ),
            )

    def claim_next_attempt(self, worker_instance_id: str) -> RepositoryAttempt | None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id,run_id FROM repository_attempts WHERE status='pending' ORDER BY requested_at,id LIMIT 1"
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """UPDATE repository_attempts SET status='running',stage='validating',
                       started_at=?,worker_instance_id=? WHERE id=? AND status='pending'""",
                    (now, worker_instance_id, row["id"]),
                )
                if cursor.rowcount != 1:
                    raise AttemptStateConflictError("attempt claim lost")
                self._recompute_run(connection, row["run_id"], now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_attempt(row["id"])

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected_status: AttemptStatus | str,
        status: AttemptStatus | str,
        stage: AttemptStage | str,
        expected_stage: AttemptStage | str | None = None,
        progress: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> RepositoryAttempt:
        expected, target = AttemptStatus(expected_status), AttemptStatus(status)
        target_stage = AttemptStage(stage)
        if expected in TERMINAL_ATTEMPT_STATUSES:
            raise AttemptStateConflictError("terminal attempts cannot be reopened")
        if target == AttemptStatus.PENDING:
            raise AttemptStateConflictError("attempts cannot transition back to pending")
        now = utc_now()
        terminal = target in TERMINAL_ATTEMPT_STATUSES
        sql = """UPDATE repository_attempts SET status=?,stage=?,progress_json=?,snapshot_id=?,
                 error_code=?,error_message=?,error_details_json=?,completed_at=?
                 WHERE id=? AND status=?"""
        params: list[Any] = [
            target.value, target_stage.value, _canonical_json(progress or {}), snapshot_id,
            error_code, error_message, _canonical_json(error_details or {}), now if terminal else None,
            attempt_id, expected.value,
        ]
        if expected_stage is not None:
            sql += " AND stage=?"
            params.append(AttemptStage(expected_stage).value)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT run_id FROM repository_attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                if not run_row:
                    raise KeyError(attempt_id)
                if connection.execute(sql, params).rowcount != 1:
                    raise AttemptStateConflictError("attempt state compare-and-swap failed")
                self._recompute_run(connection, run_row["run_id"], now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_attempt(attempt_id)  # type: ignore[return-value]

    def request_cancel(self, attempt_id: str) -> RepositoryAttempt:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status,stage,run_id FROM repository_attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                if not row:
                    raise KeyError(attempt_id)
                if row["status"] == AttemptStatus.PENDING.value:
                    connection.execute(
                        """UPDATE repository_attempts SET status='cancelled',stage='finished',
                           cancellation_requested_at=?,completed_at=? WHERE id=?""",
                        (now, now, attempt_id),
                    )
                elif row["status"] == AttemptStatus.RUNNING.value and row["stage"] != "publishing":
                    connection.execute(
                        "UPDATE repository_attempts SET cancellation_requested_at=? WHERE id=?",
                        (now, attempt_id),
                    )
                self._recompute_run(connection, row["run_id"], now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_attempt(attempt_id)  # type: ignore[return-value]

    def recover_interrupted(self) -> int:
        """Interrupt work orphaned by a previous process and recompute its runs."""
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT id,run_id FROM repository_attempts WHERE status IN ('pending','running')"
                ).fetchall()
                connection.execute(
                    """UPDATE repository_attempts SET status='interrupted',completed_at=?,
                       error_code=COALESCE(error_code,'service_restarted')
                       WHERE status IN ('pending','running')""",
                    (now,),
                )
                for run_id in {row["run_id"] for row in rows}:
                    self._recompute_run(connection, run_id, now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return len(rows)

    def publish_snapshot(
        self,
        attempt_id: str,
        evidence: EvidenceBundle,
        snapshot: RepositoryAnalysisSnapshot,
    ) -> RepositoryAnalysisSnapshot:
        """Atomically publish one member result and finish its attempt.

        Artifact durability is an adapter precondition: ``artifact_key`` must
        already identify a fully published, verified artifact.
        """
        if snapshot.evidence_digest != evidence.digest:
            raise SnapshotStoreError("snapshot and evidence digests do not match")
        if evidence.byte_size < 0 or evidence.capture_completeness not in {
            "complete", "incomplete"
        }:
            raise SnapshotStoreError("invalid evidence metadata")
        if snapshot.facts_storage not in {"inline", "artifact"}:
            raise SnapshotStoreError("invalid facts storage")
        if (snapshot.facts_storage == "inline") != (snapshot.facts is not None):
            raise SnapshotStoreError("inline facts require a JSON value")
        if (snapshot.facts_storage == "artifact") != (snapshot.facts_artifact_key is not None):
            raise SnapshotStoreError("artifact facts require an artifact key")
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                attempt = connection.execute(
                    """SELECT a.*,m.retired_at FROM repository_attempts a
                       JOIN repository_members m ON m.id=a.member_id WHERE a.id=?""",
                    (attempt_id,),
                ).fetchone()
                if not attempt:
                    raise KeyError(attempt_id)
                if attempt["status"] != AttemptStatus.RUNNING.value:
                    raise AttemptStateConflictError("only a running attempt can publish")
                if attempt["cancellation_requested_at"] is not None:
                    raise AttemptStateConflictError("a cancelled attempt cannot publish")
                if attempt["member_id"] != snapshot.member_id:
                    raise SnapshotStoreError("attempt and snapshot members do not match")

                evidence_values = (
                    evidence.digest, evidence.artifact_key, evidence.manifest_schema_version,
                    evidence.source_digest, evidence.graph_digest, evidence.graph_blob_sha256,
                    evidence.byte_size, evidence.capture_policy_digest,
                    evidence.capture_completeness, evidence.created_at or now,
                )
                connection.execute(
                    """INSERT INTO evidence_bundles
                       (digest,artifact_key,manifest_schema_version,source_digest,graph_digest,
                        graph_blob_sha256,byte_size,capture_policy_digest,capture_completeness,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING""",
                    evidence_values,
                )
                stored_evidence = connection.execute(
                    """SELECT digest,artifact_key,manifest_schema_version,source_digest,graph_digest,
                              graph_blob_sha256,byte_size,capture_policy_digest,capture_completeness,created_at
                       FROM evidence_bundles WHERE digest=?""",
                    (evidence.digest,),
                ).fetchone()
                # created_at is provenance rather than identity; every other
                # persisted value for a reused digest must be identical.
                if tuple(stored_evidence)[:-1] != evidence_values[:-1]:
                    raise SnapshotStoreError("evidence digest collision or inconsistent metadata")

                identity = connection.execute(
                    """SELECT id FROM repository_analysis_snapshots
                       WHERE member_id=? AND evidence_digest=? AND analyzer_bundle_digest=?
                         AND rules_digest=? AND report_schema_version=? AND options_digest=?""",
                    (
                        snapshot.member_id, snapshot.evidence_digest,
                        snapshot.analyzer_bundle_digest, snapshot.rules_digest,
                        snapshot.report_schema_version, snapshot.options_digest,
                    ),
                ).fetchone()
                orphaned = attempt["retired_at"] is not None
                if identity:
                    snapshot_id = identity["id"]
                    final_status = AttemptStatus.UNCHANGED
                else:
                    snapshot_id = snapshot.id
                    final_status = AttemptStatus.COMPLETED
                    connection.execute(
                        """INSERT INTO repository_analysis_snapshots
                           (id,member_id,evidence_digest,captured_at,observed_branch,
                            observed_head_commit,dirty,codegraph_version,codegraph_schema_digest,
                            analyzer_bundle_digest,rules_digest,report_schema_version,options_digest,
                            facts_digest,facts_storage,facts_json,facts_artifact_key,
                            analysis_completeness,orphaned)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            snapshot_id, snapshot.member_id, snapshot.evidence_digest,
                            snapshot.captured_at or now, snapshot.observed_branch,
                            snapshot.observed_head_commit, int(snapshot.dirty),
                            snapshot.codegraph_version, snapshot.codegraph_schema_digest,
                            snapshot.analyzer_bundle_digest, snapshot.rules_digest,
                            snapshot.report_schema_version, snapshot.options_digest,
                            snapshot.facts_digest, snapshot.facts_storage,
                            _canonical_json(snapshot.facts) if snapshot.facts is not None else None,
                            snapshot.facts_artifact_key, snapshot.analysis_completeness,
                            int(orphaned),
                        ),
                    )
                    connection.execute(
                        """INSERT INTO repository_snapshot_metadata
                           (snapshot_id,label,note,pinned,updated_at) VALUES(?,?,?,?,?)""",
                        (snapshot_id, snapshot.label, snapshot.note, int(snapshot.pinned), now),
                    )
                    if snapshot.pinned:
                        self._insert_reference(
                            connection, snapshot_id, "snapshot_pin", snapshot_id, "permanent", now, None
                        )

                if not orphaned:
                    current = connection.execute(
                        "SELECT snapshot_id FROM current_repository_snapshots WHERE member_id=?",
                        (snapshot.member_id,),
                    ).fetchone()
                    if not current or current["snapshot_id"] != snapshot_id:
                        connection.execute(
                            """INSERT INTO current_repository_snapshots(member_id,snapshot_id,published_at)
                               VALUES(?,?,?) ON CONFLICT(member_id) DO UPDATE SET
                               snapshot_id=excluded.snapshot_id,published_at=excluded.published_at""",
                            (snapshot.member_id, snapshot_id, now),
                        )
                        connection.execute(
                            "DELETE FROM snapshot_references WHERE owner_type='current' AND owner_id=?",
                            (snapshot.member_id,),
                        )
                        self._insert_reference(
                            connection, snapshot_id, "current", snapshot.member_id,
                            "permanent", now, None,
                        )
                connection.execute(
                    """UPDATE repository_attempts SET status=?,stage='finished',snapshot_id=?,
                       completed_at=? WHERE id=? AND status='running'""",
                    (final_status.value, snapshot_id, now, attempt_id),
                )
                self._recompute_run(connection, attempt["run_id"], now=now)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_snapshot(snapshot_id)  # type: ignore[return-value]

    def get_snapshot(self, snapshot_id: str) -> RepositoryAnalysisSnapshot | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT s.*,m.label,m.note,m.pinned,m.metadata_version
                   FROM repository_analysis_snapshots s
                   JOIN repository_snapshot_metadata m ON m.snapshot_id=s.id WHERE s.id=?""",
                (snapshot_id,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def get_evidence(self, digest: str) -> EvidenceBundle | None:
        """Return immutable evidence metadata for an already published snapshot."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_bundles WHERE digest=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return EvidenceBundle(
            digest=row["digest"], artifact_key=row["artifact_key"],
            manifest_schema_version=row["manifest_schema_version"],
            source_digest=row["source_digest"], graph_digest=row["graph_digest"],
            graph_blob_sha256=row["graph_blob_sha256"], byte_size=row["byte_size"],
            capture_policy_digest=row["capture_policy_digest"],
            capture_completeness=row["capture_completeness"], created_at=row["created_at"],
            deletion_state=row["deletion_state"],
        )

    def add_snapshot_reference(
        self, snapshot_id: str, owner_type: str, owner_id: str, *, state: str = "temporary"
    ) -> None:
        """Protect an active snapshot while a derived record is being created."""
        if owner_type not in {"node_rule", "business_rule", "api_explanation", "artifact_job"}:
            raise SnapshotStoreError("unsupported snapshot reference owner")
        with self.connection() as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM repository_analysis_snapshots WHERE id=? AND deletion_state='active'",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            self._insert_reference(connection, snapshot_id, owner_type, owner_id, state, utc_now(), None)

    def remove_snapshot_reference(self, snapshot_id: str, owner_type: str, owner_id: str) -> None:
        with self.connection() as connection, connection:
            connection.execute(
                "DELETE FROM snapshot_references WHERE snapshot_id=? AND owner_type=? AND owner_id=?",
                (snapshot_id, owner_type, owner_id),
            )

    def make_snapshot_reference_permanent(self, snapshot_id: str, owner_type: str, owner_id: str) -> None:
        with self.connection() as connection, connection:
            connection.execute(
                "UPDATE snapshot_references SET state='permanent',expires_at=NULL WHERE snapshot_id=? AND owner_type=? AND owner_id=?",
                (snapshot_id, owner_type, owner_id),
            )

    def create_rule_candidate(
        self, *, rule_kind: str, snapshot_id: str, subject_key: str,
        prompt: str, input_digest: str, now: str, view_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a candidate and its temporary protection in one transaction."""
        if rule_kind not in {"node", "business"}:
            raise SnapshotStoreError("invalid rule kind")
        candidate_id = str(uuid4())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM repository_analysis_snapshots WHERE id=? AND deletion_state='active'",
                    (snapshot_id,),
                ).fetchone():
                    raise KeyError(snapshot_id)
                connection.execute(
                    """INSERT INTO snapshot_rule_candidates
                       (id,rule_kind,repository_snapshot_id,view_id,subject_key,prompt,input_digest,status,created_at)
                       VALUES(?,?,?,?,?,?,?,'running',?)""",
                    (candidate_id, rule_kind, snapshot_id, view_id, subject_key, prompt, input_digest, now),
                )
                self._insert_reference(connection, snapshot_id, rule_kind + "_rule", candidate_id, "temporary", now, None)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_rule_candidate(candidate_id)  # type: ignore[return-value]

    def finish_rule_candidate(
        self, candidate_id: str, *, status: str, now: str,
        result: str | None = None, error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise SnapshotStoreError("invalid terminal rule status")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM snapshot_rule_candidates WHERE id=?", (candidate_id,)).fetchone()
                if row is None:
                    raise KeyError(candidate_id)
                if row["status"] != "running":
                    raise AttemptStateConflictError("rule candidate is already terminal")
                connection.execute(
                    "UPDATE snapshot_rule_candidates SET status=?,result=?,error=?,completed_at=? WHERE id=?",
                    (status, result, error, now, candidate_id),
                )
                if status == "completed":
                    connection.execute(
                        """INSERT INTO snapshot_rule_current(rule_kind,repository_snapshot_id,view_id,subject_key,candidate_id)
                           VALUES(?,?,?,?,?) ON CONFLICT(rule_kind,repository_snapshot_id,view_id,subject_key)
                           DO UPDATE SET candidate_id=excluded.candidate_id""",
                        (row["rule_kind"], row["repository_snapshot_id"], row["view_id"] or "", row["subject_key"], candidate_id),
                    )
                    connection.execute("UPDATE snapshot_references SET state='permanent',expires_at=NULL WHERE owner_id=? AND owner_type=?", (candidate_id, row["rule_kind"] + "_rule"))
                else:
                    connection.execute("DELETE FROM snapshot_references WHERE owner_id=? AND owner_type=?", (candidate_id, row["rule_kind"] + "_rule"))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_rule_candidate(candidate_id)  # type: ignore[return-value]

    def get_rule_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM snapshot_rule_candidates WHERE id=?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def get_current_rule(self, *, rule_kind: str, snapshot_id: str, subject_key: str, view_id: str | None = None) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT c.* FROM snapshot_rule_current p JOIN snapshot_rule_candidates c ON c.id=p.candidate_id
                   WHERE p.rule_kind=? AND p.repository_snapshot_id=? AND p.view_id=? AND p.subject_key=?""",
                (rule_kind, snapshot_id, view_id or "", subject_key),
            ).fetchone()
        return dict(row) if row else None

    def list_current_rules(self, *, rule_kind: str, snapshot_id: str, view_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT c.* FROM snapshot_rule_current p JOIN snapshot_rule_candidates c ON c.id=p.candidate_id
                   WHERE p.rule_kind=? AND p.repository_snapshot_id=? AND p.view_id=? ORDER BY c.subject_key""",
                (rule_kind, snapshot_id, view_id or ""),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── LLM knowledge extraction jobs ──

    @staticmethod
    def _llm_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["progress"] = json.loads(item.pop("progress_json") or "{}")
        item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        item.pop("result_json", None)
        return item

    def get_llm_knowledge_job(self, snapshot_id: str, *, job_id: str | None = None) -> dict[str, Any] | None:
        with self.connection() as connection:
            if job_id:
                row = connection.execute(
                    "SELECT * FROM llm_knowledge_jobs WHERE id=? AND repository_snapshot_id=?",
                    (job_id, snapshot_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM llm_knowledge_jobs WHERE repository_snapshot_id=? "
                    "ORDER BY requested_at DESC, id DESC LIMIT 1",
                    (snapshot_id,),
                ).fetchone()
        return self._llm_job(row)

    def create_llm_knowledge_job(self, snapshot_id: str) -> dict[str, Any]:
        job_id = str(uuid4())
        now = utc_now()
        progress = {"percent": 0, "completed": 0, "total": 4, "stage": "queued"}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM repository_analysis_snapshots WHERE id=? AND deletion_state='active'",
                    (snapshot_id,),
                ).fetchone():
                    raise KeyError(snapshot_id)
                active = connection.execute(
                    "SELECT * FROM llm_knowledge_jobs WHERE repository_snapshot_id=? "
                    "AND status IN ('pending','running') ORDER BY requested_at DESC LIMIT 1",
                    (snapshot_id,),
                ).fetchone()
                if active is not None:
                    connection.commit()
                    return self._llm_job(active)  # type: ignore[return-value]
                connection.execute(
                    """INSERT INTO llm_knowledge_jobs
                       (id,repository_snapshot_id,status,progress_json,requested_at)
                       VALUES (?,?, 'pending', ?, ?)""",
                    (job_id, snapshot_id, _canonical_json(progress), now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_llm_knowledge_job(snapshot_id, job_id=job_id)  # type: ignore[return-value]

    def update_llm_knowledge_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if status is not None and status not in {"pending", "running", "completed", "failed"}:
            raise SnapshotStoreError("invalid LLM knowledge job status")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM llm_knowledge_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            next_status = status or row["status"]
            next_progress = progress if progress is not None else json.loads(row["progress_json"] or "{}")
            next_result = result if result is not None else (
                json.loads(row["result_json"]) if row["result_json"] else None
            )
            started_at = row["started_at"] or (utc_now() if next_status == "running" else None)
            completed_at = utc_now() if next_status in {"completed", "failed"} else row["completed_at"]
            connection.execute(
                """UPDATE llm_knowledge_jobs SET status=?,progress_json=?,result_json=?,error_message=?,
                   started_at=?,completed_at=? WHERE id=?""",
                (
                    next_status, _canonical_json(next_progress),
                    _canonical_json(next_result) if next_result is not None else None,
                    error_message if error_message is not None else row["error_message"],
                    started_at, completed_at, job_id,
                ),
            )
            connection.commit()
        return self.get_llm_knowledge_job(row["repository_snapshot_id"], job_id=job_id)

    def recover_interrupted_llm_knowledge_jobs(self) -> list[str]:
        """Re-queue jobs left active by a process restart."""
        with self.connection() as connection, connection:
            rows = connection.execute(
                "SELECT id FROM llm_knowledge_jobs WHERE status IN ('pending','running')"
            ).fetchall()
            connection.execute(
                "UPDATE llm_knowledge_jobs SET status='pending',started_at=NULL,completed_at=NULL "
                "WHERE status IN ('pending','running')"
            )
        return [row["id"] for row in rows]

    def list_pending_llm_knowledge_jobs(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM llm_knowledge_jobs WHERE status='pending' ORDER BY requested_at,id"
            ).fetchall()
        return [self._llm_job(row) for row in rows]  # type: ignore[list-item]

    def create_artifact_job(
        self, *, view_id: str, artifact_kind: str, cache_key: str,
        request_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = str(uuid4())
        now = utc_now()
        request_spec_json = _canonical_json(request_spec or {})
        if len(request_spec_json.encode("utf-8")) > MAX_ARTIFACT_REQUEST_SPEC_BYTES:
            raise SnapshotStoreError("artifact_request_spec_too_large")
        request_spec_digest = "sha256:" + sha256(request_spec_json.encode()).hexdigest()
        created = False
        with self.connection() as connection, connection:
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE cache_key_digest=? ORDER BY attempt_no DESC LIMIT 1", (cache_key,)).fetchone()
            if row is None or row["status"] in ("failed", "cancelled", "interrupted"):
                attempt = (int(row["attempt_no"]) + 1) if row is not None else 1
                retry_of = row["id"] if row is not None else None
                connection.execute(
                    """INSERT INTO graph_view_artifact_jobs
                       (id,view_id,cache_key_digest,artifact_kind,attempt_no,retry_of_job_id,status,requested_at,request_spec_json,request_spec_digest)
                       VALUES(?,?,?,?,?,?, 'pending',?,?,?)""",
                    (job_id, view_id, cache_key, artifact_kind, attempt, retry_of, now,
                     request_spec_json, request_spec_digest),
                )
                created = True
                row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
                # Protect every input snapshot while the job is active.
                snapshots = connection.execute(
                    "SELECT snapshot_id FROM graph_view_members WHERE view_id=? AND snapshot_id IS NOT NULL",
                    (view_id,),
                ).fetchall()
                for item in snapshots:
                    self._insert_reference(connection, item["snapshot_id"], "artifact_job", job_id, "temporary", now, None)
        result = dict(row)
        result["reused"] = not created
        return result

    def get_artifact_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def public_artifact_job(job: dict[str, Any]) -> dict[str, Any]:
        """Return the bounded job contract safe for an HTTP/MCP caller.

        The persisted row may contain an inline topology payload.  Returning
        that payload from the job-status endpoint both duplicates the cache
        response and makes a status read an authorization bypass.  Delivery
        always goes through the View-bound artifact endpoint instead.
        """
        result = dict(job)
        result.pop("payload_json", None)
        error = result.get("error_message")
        if isinstance(error, str):
            result["error_message"] = error[:MAX_ARTIFACT_ERROR_BYTES]
        return result

    def list_artifact_jobs(
        self, view_id: str, *, artifact_kind: str = "topology", limit: int = 20
    ) -> list[dict[str, Any]]:
        """List durable generation attempts without exposing payload contents."""
        bounded = max(1, min(int(limit), 100))
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM graph_view_artifact_jobs
                   WHERE view_id=? AND artifact_kind=?
                   ORDER BY requested_at DESC, attempt_no DESC LIMIT ?""",
                (view_id, artifact_kind, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_artifact_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM graph_view_artifact_cache WHERE cache_key_digest=?", (cache_key,)).fetchone()
        return dict(row) if row else None

    def touch_artifact_cache(self, cache_key: str) -> None:
        with self.connection() as connection, connection:
            connection.execute(
                "UPDATE graph_view_artifact_cache SET last_accessed_at=? WHERE cache_key_digest=?",
                (utc_now(), cache_key),
            )

    def acquire_artifact_cache_reader(
        self,
        cache_key: str,
        *,
        lease_seconds: int = DEFAULT_TOPOLOGY_READER_LEASE_SECONDS,
        reader_id: str | None = None,
    ) -> str:
        """Acquire a short reader lease before opening a CAS payload.

        Cache scavenging runs in a separate process/thread.  The lease gives
        it a durable exclusion check instead of relying on an in-memory lock.
        A missing cache is rejected while holding the same transaction that
        inserts the lease, closing the select/delete race.
        """
        token = reader_id or str(uuid4())
        seconds = max(1, min(int(lease_seconds), 3600))
        now_dt = datetime.now(timezone.utc)
        expires = (now_dt + timedelta(seconds=seconds)).isoformat(timespec="microseconds")
        with self.connection() as connection, connection:
            if connection.execute(
                "SELECT 1 FROM graph_view_artifact_cache WHERE cache_key_digest=?",
                (cache_key,),
            ).fetchone() is None:
                raise KeyError(cache_key)
            connection.execute(
                "INSERT OR REPLACE INTO graph_view_artifact_readers(cache_key_digest,reader_id,lease_until) VALUES(?,?,?)",
                (cache_key, token, expires),
            )
        return token

    def renew_artifact_cache_reader(
        self,
        cache_key: str,
        reader_id: str,
        *,
        lease_seconds: int = DEFAULT_TOPOLOGY_READER_LEASE_SECONDS,
    ) -> None:
        seconds = max(1, min(int(lease_seconds), 3600))
        expires = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")
        with self.connection() as connection, connection:
            updated = connection.execute(
                "UPDATE graph_view_artifact_readers SET lease_until=? WHERE cache_key_digest=? AND reader_id=?",
                (expires, cache_key, reader_id),
            )
            if updated.rowcount != 1:
                raise SnapshotStoreError("artifact_reader_lease_missing")

    def release_artifact_cache_reader(self, cache_key: str, reader_id: str) -> None:
        with self.connection() as connection, connection:
            connection.execute(
                "DELETE FROM graph_view_artifact_readers WHERE cache_key_digest=? AND reader_id=?",
                (cache_key, reader_id),
            )

    @contextmanager
    def artifact_cache_reader(
        self,
        cache_key: str,
        *,
        lease_seconds: int = DEFAULT_TOPOLOGY_READER_LEASE_SECONDS,
    ) -> Iterator[str]:
        """Hold a reader lease for the duration of a cache/CAS read."""
        reader_id = self.acquire_artifact_cache_reader(
            cache_key, lease_seconds=lease_seconds
        )
        try:
            yield reader_id
        finally:
            self.release_artifact_cache_reader(cache_key, reader_id)

    def scavenge_artifact_cache(
        self,
        *,
        max_age_seconds: int = DEFAULT_TOPOLOGY_CACHE_RETENTION_SECONDS,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Remove cold cache metadata using TTL + LRU ordering.

        Active jobs and active reader leases are both exclusion conditions.  A
        returned row retains its ``artifact_key`` so a caller may run a
        separate CAS sweep after checking references; this catalog operation
        never removes a payload behind a reader's back.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0, max_age_seconds))).isoformat(timespec="microseconds")
        bounded = max(1, min(int(limit), 1000))
        with self.connection() as connection, connection:
            now = utc_now()
            connection.execute(
                "DELETE FROM graph_view_artifact_readers WHERE lease_until<=?",
                (now,),
            )
            rows = connection.execute(
                """SELECT c.* FROM graph_view_artifact_cache c
                   WHERE c.last_accessed_at<?
                   AND NOT EXISTS (
                       SELECT 1 FROM graph_view_artifact_jobs j
                       WHERE j.cache_key_digest=c.cache_key_digest AND j.status IN ('pending','running')
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM graph_view_artifact_readers r
                       WHERE r.cache_key_digest=c.cache_key_digest AND r.lease_until>?
                   )
                   ORDER BY c.last_accessed_at LIMIT ?""",
                (cutoff, now, bounded),
            ).fetchall()
            result = [dict(row) for row in rows]
            for row in result:
                connection.execute(
                    "DELETE FROM graph_view_artifact_cache WHERE cache_key_digest=?",
                    (row["cache_key_digest"],),
                )
        return result

    def scavenge_artifact_jobs(
        self,
        *,
        max_age_seconds: int = DEFAULT_TOPOLOGY_JOB_RETENTION_SECONDS,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Delete old terminal Job metadata without breaking retry history.

        A retry row points at its previous attempt with ``RESTRICT``.  We
        therefore only remove terminal rows with no child retry; the next
        bounded pass can remove older ancestors after their children are gone.
        Active jobs and their temporary Snapshot references are never touched.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0, int(max_age_seconds)))).isoformat(timespec="microseconds")
        bounded = max(1, min(int(limit), 1000))
        with self.connection() as connection, connection:
            rows = connection.execute(
                """SELECT j.* FROM graph_view_artifact_jobs j
                   WHERE j.status IN ('completed','failed','cancelled','interrupted')
                     AND COALESCE(j.completed_at,j.requested_at)<?
                     AND NOT EXISTS (
                       SELECT 1 FROM graph_view_artifact_jobs child
                       WHERE child.retry_of_job_id=j.id
                     )
                   ORDER BY COALESCE(j.completed_at,j.requested_at),j.id LIMIT ?""",
                (cutoff, bounded),
            ).fetchall()
            result = [dict(row) for row in rows]
            for row in result:
                # Defensive cleanup for databases upgraded from an older
                # release where a terminal worker might have leaked a ref.
                connection.execute(
                    "DELETE FROM snapshot_references WHERE owner_type='artifact_job' AND owner_id=?",
                    (row["id"],),
                )
                connection.execute(
                    "DELETE FROM graph_view_artifact_jobs WHERE id=? AND status IN ('completed','failed','cancelled','interrupted')",
                    (row["id"],),
                )
        return result

    def snapshot_references(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,owner_type,owner_id,state,created_at,expires_at FROM snapshot_references WHERE snapshot_id=? ORDER BY owner_type,owner_id",
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def request_snapshot_deletion(self, snapshot_id: str) -> dict[str, Any]:
        job_id, now = str(uuid4()), utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                snapshot = connection.execute("SELECT * FROM repository_analysis_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if snapshot is None:
                    raise KeyError(snapshot_id)
                if connection.execute("SELECT 1 FROM current_repository_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone():
                    raise SnapshotStoreError("snapshot_protected")
                refs = connection.execute("SELECT 1 FROM snapshot_references WHERE snapshot_id=? LIMIT 1", (snapshot_id,)).fetchone()
                if refs:
                    raise SnapshotStoreError("snapshot_protected")
                connection.execute("UPDATE repository_analysis_snapshots SET deletion_state='deletion_pending' WHERE id=?", (snapshot_id,))
                connection.execute("INSERT INTO deletion_jobs VALUES(?,?,?,'pending',?,NULL,NULL,NULL)", (job_id, "snapshot", snapshot_id, now))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"id": job_id, "target_type": "snapshot", "target_id": snapshot_id, "status": "pending"}

    def get_deletion_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM deletion_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_deletion_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM deletion_jobs WHERE status=? ORDER BY requested_at,id", (status,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM deletion_jobs ORDER BY requested_at,id").fetchall()
        return [dict(row) for row in rows]

    def metrics(self) -> dict[str, Any]:
        tables = {
            "attempts": "repository_attempts",
            "snapshots": "repository_analysis_snapshots",
            "views": "graph_views",
            "artifact_jobs": "graph_view_artifact_jobs",
            "deletion_jobs": "deletion_jobs",
            "storage_reservations": "storage_reservations",
        }
        result: dict[str, Any] = {}
        with self.connection() as connection:
            for name, table in tables.items():
                columns = {item["name"] for item in connection.execute(f"PRAGMA table_info({table})")}
                if "status" not in columns:
                    rows = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchall()
                    result[name] = int(rows[0]["count"])
                else:
                    rows = connection.execute(f"SELECT status,COUNT(*) AS count FROM {table} GROUP BY status").fetchall()
                    result[name] = {row["status"]: int(row["count"]) for row in rows}
            result["active_attempts"] = int(connection.execute("SELECT COUNT(*) FROM repository_attempts WHERE status IN ('pending','running')").fetchone()[0])
        return result

    def mark_deletion_trashed(self, job_id: str, trash_path: str) -> None:
        with self.connection() as connection, connection:
            row = connection.execute("SELECT target_id FROM deletion_jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            connection.execute("UPDATE repository_analysis_snapshots SET deletion_state='trashed' WHERE id=?", (row["target_id"],))
            connection.execute("UPDATE deletion_jobs SET status='trashed',trash_path=? WHERE id=?", (trash_path, job_id))

    def complete_deletion(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection, connection:
            updated = connection.execute(
                "UPDATE deletion_jobs SET status='completed',completed_at=? WHERE id=? AND status IN ('pending','trashed')",
                (utc_now(), job_id),
            )
            if updated.rowcount != 1:
                raise SnapshotStoreError("deletion job is not in trashed state")
            row = connection.execute("SELECT * FROM deletion_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def complete_artifact_job(
        self, job_id: str, payload: dict[str, Any], artifact_key: str | None = None,
        *, lease_token: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection, connection:
            existing = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
            if existing is None:
                raise KeyError(job_id)
            lease_clause = " AND lease_token=?" if lease_token else ""
            params: tuple[Any, ...] = (_canonical_json(payload), utc_now(), job_id)
            if lease_token:
                params += (lease_token,)
            updated = connection.execute(
                f"UPDATE graph_view_artifact_jobs SET status='completed',payload_json=?,completed_at=?,stage='finished',lease_until=NULL WHERE id=? AND status IN ('pending','running'){lease_clause}",
                params,
            )
            if updated.rowcount != 1:
                raise SnapshotStoreError("artifact job is not active")
            now = utc_now()
            payload_json = _canonical_json(payload)
            digest_value = dict(payload)
            digest_value.pop("payload_digest", None)
            payload_digest = "sha256:" + sha256(_canonical_json(digest_value).encode()).hexdigest()
            storage = "artifact" if artifact_key else "inline"
            connection.execute(
                """INSERT OR REPLACE INTO graph_view_artifact_cache
                   (cache_key_digest,view_digest,artifact_kind,created_at,last_accessed_at,payload_storage,payload_json,artifact_key,payload_digest,byte_size)
                   SELECT j.cache_key_digest, v.digest, j.artifact_kind, ?, ?, ?, ?, ?, ?, ?
                   FROM graph_view_artifact_jobs j JOIN graph_views v ON v.id=j.view_id WHERE j.id=?""",
                (now, now, storage, None if artifact_key else payload_json, artifact_key,
                 payload_digest, len(payload_json.encode()), job_id),
            )
            connection.execute("DELETE FROM snapshot_references WHERE owner_type='artifact_job' AND owner_id=?", (job_id,))
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def start_artifact_job(
        self, job_id: str, *, worker_id: str = "graph-artifact-worker", lease_seconds: int = 300
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="microseconds")
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds")
        lease_token = str(uuid4())
        with self.connection() as connection, connection:
            connection.execute(
                """UPDATE graph_view_artifact_jobs
                   SET status='running',started_at=COALESCE(started_at,?),worker_id=?,
                       lease_token=?,lease_until=?,heartbeat_at=?,stage='building'
                   WHERE id=? AND status='pending'""",
                (now, worker_id, lease_token, lease_until, now, job_id),
            )
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def heartbeat_artifact_job(self, job_id: str, lease_token: str, *, lease_seconds: int = 300) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="microseconds")
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds")
        with self.connection() as connection, connection:
            updated = connection.execute(
                """UPDATE graph_view_artifact_jobs SET heartbeat_at=?,lease_until=?
                   WHERE id=? AND status='running' AND lease_token=?""",
                (now, lease_until, job_id, lease_token),
            )
            if updated.rowcount != 1:
                raise SnapshotStoreError("artifact lease fencing failed")
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def recover_interrupted_artifact_jobs(self) -> int:
        now = utc_now()
        with self.connection() as connection, connection:
            updated = connection.execute(
                """UPDATE graph_view_artifact_jobs
                   SET status='interrupted',completed_at=?,error_code='lease_expired',stage='finished'
                   WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?""",
                (now, now),
            )
        return updated.rowcount

    def cancel_artifact_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection, connection:
            connection.execute("UPDATE graph_view_artifact_jobs SET status='cancelled',completed_at=?,stage='finished',lease_until=NULL WHERE id=? AND status IN ('pending','running')", (utc_now(), job_id))
            connection.execute("DELETE FROM snapshot_references WHERE owner_type='artifact_job' AND owner_id=?", (job_id,))
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def fail_artifact_job(
        self, job_id: str, error: str, *, code: str = "artifact_failed", lease_token: str | None = None
    ) -> dict[str, Any]:
        with self.connection() as connection, connection:
            lease_clause = " AND lease_token=?" if lease_token else ""
            params: tuple[Any, ...] = (code, error[:2000], utc_now(), job_id)
            if lease_token:
                params += (lease_token,)
            updated = connection.execute(
                f"UPDATE graph_view_artifact_jobs SET status='failed',error_code=?,error_message=?,completed_at=?,stage='finished',lease_until=NULL WHERE id=? AND status IN ('pending','running'){lease_clause}",
                params,
            )
            if updated.rowcount != 1:
                raise SnapshotStoreError("artifact job is not active")
            connection.execute("DELETE FROM snapshot_references WHERE owner_type='artifact_job' AND owner_id=?", (job_id,))
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def retry_artifact_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in ("failed", "cancelled", "interrupted"):
                return dict(row)
            new_id, now = str(uuid4()), utc_now()
            with connection:
                connection.execute(
                    """INSERT INTO graph_view_artifact_jobs
                       (id,view_id,cache_key_digest,artifact_kind,attempt_no,retry_of_job_id,status,requested_at,request_spec_json,request_spec_digest)
                       VALUES(?,?,?,?,?,?, 'pending',?,?,?)""",
                    (new_id, row["view_id"], row["cache_key_digest"], row["artifact_kind"], int(row["attempt_no"])+1, job_id, now,
                     row["request_spec_json"], row["request_spec_digest"]),
                )
                snapshots = connection.execute(
                    "SELECT snapshot_id FROM graph_view_members WHERE view_id=? AND snapshot_id IS NOT NULL",
                    (row["view_id"],),
                ).fetchall()
                for item in snapshots:
                    self._insert_reference(connection, item["snapshot_id"], "artifact_job", new_id, "temporary", now, None)
                created = connection.execute("SELECT * FROM graph_view_artifact_jobs WHERE id=?", (new_id,)).fetchone()
        return dict(created)

    def reserve_storage(self, attempt_id: str, bytes_needed: int, available_bytes: int, expires_at: str) -> None:
        if bytes_needed < 0:
            raise SnapshotStoreError("insufficient_storage")
        with self.connection() as connection, connection:
            reserved = connection.execute(
                "SELECT COALESCE(SUM(reserved_bytes),0) FROM storage_reservations WHERE expires_at>? AND attempt_id<>?",
                (utc_now(), attempt_id),
            ).fetchone()[0]
            if bytes_needed > max(0, available_bytes - int(reserved)):
                raise SnapshotStoreError("insufficient_storage")
            connection.execute("DELETE FROM storage_reservations WHERE attempt_id=?", (attempt_id,))
            connection.execute(
                "INSERT INTO storage_reservations(attempt_id,reserved_bytes,created_at,expires_at) VALUES(?,?,?,?)",
                (attempt_id, bytes_needed, utc_now(), expires_at),
            )

    def release_storage(self, attempt_id: str) -> None:
        with self.connection() as connection, connection:
            connection.execute("DELETE FROM storage_reservations WHERE attempt_id=?", (attempt_id,))

    def set_snapshot_reference_state(
        self, snapshot_id: str, owner_type: str, owner_id: str, *, state: str | None
    ) -> None:
        """Promote a successful derived record or release a failed candidate."""
        with self.connection() as connection, connection:
            if state is None:
                connection.execute(
                    "DELETE FROM snapshot_references WHERE snapshot_id=? AND owner_type=? AND owner_id=?",
                    (snapshot_id, owner_type, owner_id),
                )
            else:
                connection.execute(
                    "UPDATE snapshot_references SET state=?,expires_at=NULL WHERE snapshot_id=? AND owner_type=? AND owner_id=?",
                    (state, snapshot_id, owner_type, owner_id),
                )

    def list_snapshots(self, member_id: str) -> list[RepositoryAnalysisSnapshot]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.*,m.label,m.note,m.pinned,m.metadata_version
                   FROM repository_analysis_snapshots s
                   JOIN repository_snapshot_metadata m ON m.snapshot_id=s.id
                   WHERE s.member_id=? ORDER BY s.captured_at DESC,s.id DESC""",
                (member_id,),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def retention_candidates(self, member_id: str, keep: int = 10) -> list[str]:
        """Return old successful snapshots that are safe to enqueue for deletion."""
        if keep < 0:
            raise ValueError("keep must be non-negative")
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.id FROM repository_analysis_snapshots s
                   WHERE s.member_id=? AND s.analysis_completeness='complete'
                     AND s.deletion_state='active'
                     AND s.id NOT IN (SELECT snapshot_id FROM current_repository_snapshots)
                     AND s.id NOT IN (SELECT snapshot_id FROM snapshot_references)
                   ORDER BY s.captured_at DESC,s.id DESC""", (member_id,)
            ).fetchall()
        return [row["id"] for row in rows[keep:]]

    def get_current_snapshot(self, member_id: str) -> RepositoryAnalysisSnapshot | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT s.*,m.label,m.note,m.pinned,m.metadata_version
                   FROM current_repository_snapshots c
                   JOIN repository_analysis_snapshots s ON s.id=c.snapshot_id
                   JOIN repository_snapshot_metadata m ON m.snapshot_id=s.id
                   WHERE c.member_id=?""",
                (member_id,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def update_snapshot_metadata(
        self,
        snapshot_id: str,
        *,
        label: str,
        note: str,
        pinned: bool,
        expected_version: int,
    ) -> RepositoryAnalysisSnapshot:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """UPDATE repository_snapshot_metadata
                       SET label=?,note=?,pinned=?,metadata_version=metadata_version+1,updated_at=?
                       WHERE snapshot_id=? AND metadata_version=?""",
                    (label, note, int(pinned), now, snapshot_id, expected_version),
                )
                if updated.rowcount != 1:
                    if not connection.execute(
                        "SELECT 1 FROM repository_snapshot_metadata WHERE snapshot_id=?", (snapshot_id,)
                    ).fetchone():
                        raise KeyError(snapshot_id)
                    raise SnapshotStoreError("snapshot metadata version conflict")
                if pinned:
                    self._insert_reference(
                        connection, snapshot_id, "snapshot_pin", snapshot_id, "permanent", now, None
                    )
                else:
                    connection.execute(
                        """DELETE FROM snapshot_references
                           WHERE snapshot_id=? AND owner_type='snapshot_pin' AND owner_id=?""",
                        (snapshot_id, snapshot_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_snapshot(snapshot_id)  # type: ignore[return-value]

    def create_current_view(
        self,
        *,
        scope_ids: Sequence[str] | None = None,
        member_ids: Sequence[str] | None = None,
        view_id: str | None = None,
    ) -> GraphView:
        if (scope_ids is None) == (member_ids is None):
            raise SnapshotStoreError("provide exactly one current-view selector")
        with self.connection() as connection:
            if scope_ids is not None:
                if len(scope_ids) != 1 or not scope_ids[0]:
                    raise SnapshotStoreError("a graph view must select exactly one scope")
                scope_id = scope_ids[0]
                known = connection.execute(
                    "SELECT id FROM analysis_scopes WHERE id=? AND retired_at IS NULL",
                    (scope_id,),
                ).fetchall()
                if not known:
                    raise SnapshotStoreError("scope must exist and be active")
                rows = connection.execute(
                    """SELECT m.id AS member_id,m.scope_id,m.display_name,m.declared_aliases_json,
                              m.retired_at,c.snapshot_id
                        FROM repository_members m LEFT JOIN current_repository_snapshots c ON c.member_id=m.id
                        WHERE m.scope_id=? AND m.retired_at IS NULL ORDER BY m.id""",
                    (scope_id,),
                ).fetchall()
                selector = {"scope_id": scope_id}
            else:
                selected = list(member_ids or ())
                if not selected or len(set(selected)) != len(selected):
                    raise SnapshotStoreError("member_ids must be non-empty and unique")
                placeholders = ",".join("?" for _ in selected)
                rows = connection.execute(
                    f"""SELECT m.id AS member_id,m.scope_id,m.display_name,m.declared_aliases_json,
                              m.retired_at,c.snapshot_id
                        FROM repository_members m LEFT JOIN current_repository_snapshots c ON c.member_id=m.id
                        WHERE m.id IN ({placeholders}) ORDER BY m.id""",
                    tuple(selected),
                ).fetchall()
                if {row["member_id"] for row in rows} != set(selected):
                    raise SnapshotStoreError("all members must exist")
                scope_ids_found = {row["scope_id"] for row in rows}
                if len(scope_ids_found) != 1:
                    raise SnapshotStoreError("mixed_scope_members")
                scope_id = next(iter(scope_ids_found))
                selector = {"member_ids": selected, "scope_id": scope_id}
        mappings = []
        for ordinal, row in enumerate(rows):
            if row["retired_at"] is not None:
                availability, snapshot_id = ViewAvailability.RETIRED, None
            elif row["snapshot_id"] is None:
                availability, snapshot_id = ViewAvailability.UNPARSED, None
            else:
                availability, snapshot_id = ViewAvailability.AVAILABLE, row["snapshot_id"]
            mappings.append(
                GraphViewMember(
                    row["member_id"], ordinal, snapshot_id, availability,
                    row["display_name"], tuple(json.loads(row["declared_aliases_json"])),
                )
            )
        return self._create_view(mappings, scope_id=scope_id, selector=selector, view_id=view_id)

    def create_explicit_view(
        self,
        members: Sequence[GraphViewMember],
        *,
        view_id: str | None = None,
    ) -> GraphView:
        if not members or len({item.member_id for item in members}) != len(members):
            raise SnapshotStoreError("view members must be non-empty and unique")
        member_ids = [item.member_id for item in members]
        placeholders = ",".join("?" for _ in member_ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT id,scope_id,display_name,declared_aliases_json
                    FROM repository_members WHERE id IN ({placeholders})""",
                tuple(member_ids),
            ).fetchall()
        if {row["id"] for row in rows} != set(member_ids):
            raise SnapshotStoreError("all members must exist")
        scope_ids = {row["scope_id"] for row in rows}
        if len(scope_ids) != 1:
            raise SnapshotStoreError("mixed_scope_members")
        scope_id = next(iter(scope_ids))
        metadata = {row["id"]: row for row in rows}
        normalized = [
            GraphViewMember(
                item.member_id, ordinal, item.snapshot_id, item.availability,
                item.display_name or metadata[item.member_id]["display_name"],
                canonical_aliases(item.declared_aliases)
                if item.declared_aliases
                else tuple(json.loads(metadata[item.member_id]["declared_aliases_json"])),
            )
            for ordinal, item in enumerate(sorted(members, key=lambda item: item.member_id))
        ]
        return self._create_view(
            normalized, scope_id=scope_id,
            selector={"explicit": True, "scope_id": scope_id}, view_id=view_id,
        )

    def _create_view(
        self,
        members: Sequence[GraphViewMember],
        *,
        scope_id: str,
        selector: dict[str, Any],
        view_id: str | None,
    ) -> GraphView:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="microseconds")
        expires = (now_dt + timedelta(hours=24)).isoformat(timespec="microseconds")
        digest_body = {
            "schema": "graph-view/v2",
            "scope_id": scope_id,
            "topology_rules_digest": DEFAULT_TOPOLOGY_RULES_DIGEST,
            "members": [
                {"member_id": item.member_id, "snapshot_id": item.snapshot_id,
                 "availability": item.availability.value, "display_name": item.display_name,
                 "declared_aliases": list(canonical_aliases(item.declared_aliases))}
                for item in members
            ],
        }
        digest = "sha256:" + sha256(_canonical_json(digest_body).encode()).hexdigest()
        new_id = view_id or str(uuid4())
        completeness = (
            "complete" if all(item.availability == ViewAvailability.AVAILABLE for item in members)
            else "incomplete"
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO graph_views
                       (id,digest,scope_id,identity_schema,topology_rules_digest,lifecycle,completeness,created_at,last_accessed_at,expires_at,selector_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id, digest, scope_id, "graph-view/v2", DEFAULT_TOPOLOGY_RULES_DIGEST, ViewLifecycle.EPHEMERAL.value, completeness,
                     now, now, expires, _canonical_json(selector)),
                )
                for item in members:
                    connection.execute(
                        """INSERT INTO graph_view_members
                           (view_id,ordinal,member_id,snapshot_id,availability,display_name,declared_aliases_json) VALUES(?,?,?,?,?,?,?)""",
                        (new_id, item.ordinal, item.member_id, item.snapshot_id, item.availability.value,
                         item.display_name, _canonical_json(list(canonical_aliases(item.declared_aliases))),),
                    )
                    if item.snapshot_id:
                        self._insert_reference(
                            connection, item.snapshot_id, "ephemeral_view", new_id,
                            "temporary", now, expires,
                        )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise SnapshotStoreError(str(error)) from error
            except BaseException:
                connection.rollback()
                raise
        return self.get_view(new_id, touch=False)  # type: ignore[return-value]

    def get_view(self, view_id: str, *, touch: bool = True) -> GraphView | None:
        now_dt = datetime.now(timezone.utc)
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM graph_views WHERE id=?", (view_id,)).fetchone()
            if not row:
                return None
            if row["lifecycle"] == "ephemeral" and datetime.fromisoformat(row["expires_at"]) <= now_dt:
                raise ViewExpiredError(view_id)
            if touch and row["lifecycle"] == "ephemeral":
                last_accessed = datetime.fromisoformat(row["last_accessed_at"])
                if last_accessed <= now_dt - timedelta(hours=1):
                    now = now_dt.isoformat(timespec="microseconds")
                    expires = (now_dt + timedelta(hours=24)).isoformat(timespec="microseconds")
                    with connection:
                        connection.execute(
                            "UPDATE graph_views SET last_accessed_at=?,expires_at=? WHERE id=?",
                            (now, expires, view_id),
                        )
                        connection.execute(
                            """UPDATE snapshot_references SET expires_at=?
                               WHERE owner_type='ephemeral_view' AND owner_id=?""",
                            (expires, view_id),
                        )
                    row = connection.execute("SELECT * FROM graph_views WHERE id=?", (view_id,)).fetchone()
            member_rows = connection.execute(
                "SELECT * FROM graph_view_members WHERE view_id=? ORDER BY ordinal", (view_id,)
            ).fetchall()
        return self._view(row, member_rows)

    def list_views(self) -> list[GraphView]:
        """Return all graph views that are still available for browsing.

        Ephemeral views are intentionally omitted after their expiry window;
        pinned views have no expiry and remain visible in the catalog.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM graph_views
                   WHERE lifecycle='pinned' OR expires_at > ?
                   ORDER BY created_at DESC,id DESC""",
                (now,),
            ).fetchall()
            members_by_view: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                members_by_view[row["id"]] = connection.execute(
                    "SELECT * FROM graph_view_members WHERE view_id=? ORDER BY ordinal",
                    (row["id"],),
                ).fetchall()
        return [self._view(row, members_by_view[row["id"]]) for row in rows]

    def pin_view(self, view_id: str, *, label: str = "", note: str = "") -> GraphView:
        # Validate expiry before converting temporary references.
        if self.get_view(view_id, touch=False) is None:
            raise KeyError(view_id)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """UPDATE graph_views SET lifecycle='pinned',expires_at=NULL,label=?,note=?
                       WHERE id=?""",
                    (label, note, view_id),
                )
                connection.execute(
                    """UPDATE snapshot_references SET owner_type='pinned_view',state='permanent',expires_at=NULL
                       WHERE owner_type='ephemeral_view' AND owner_id=?""",
                    (view_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.get_view(view_id, touch=False)  # type: ignore[return-value]

    @staticmethod
    def _insert_reference(
        connection: sqlite3.Connection,
        snapshot_id: str,
        owner_type: str,
        owner_id: str,
        state: str,
        created_at: str,
        expires_at: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO snapshot_references
               (id,snapshot_id,owner_type,owner_id,state,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(snapshot_id,owner_type,owner_id) DO NOTHING""",
            (str(uuid4()), snapshot_id, owner_type, owner_id, state, created_at, expires_at),
        )

    def _recompute_run(self, connection: sqlite3.Connection, run_id: str, *, now: str) -> None:
        rows = connection.execute(
            """SELECT rm.disposition,a.status,a.started_at
               FROM analysis_run_members rm LEFT JOIN repository_attempts a ON a.id=rm.attempt_id
               WHERE rm.run_id=? ORDER BY rm.ordinal""",
            (run_id,),
        ).fetchall()
        outcomes = [row["status"] if row["disposition"] == "queued" else None for row in rows]
        status = aggregate_run_status(outcomes, any_started=any(row["started_at"] for row in rows))
        started = any(row["started_at"] for row in rows)
        completed_at = now if status in {
            RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED,
            RunStatus.CANCELLED, RunStatus.INTERRUPTED,
        } else None
        connection.execute(
            """UPDATE analysis_runs SET status=?,started_at=CASE WHEN ? THEN COALESCE(started_at,?) ELSE started_at END,
               completed_at=CASE WHEN completed_at IS NULL THEN ? ELSE completed_at END WHERE id=?""",
            (status.value, started, now, completed_at, run_id),
        )

    @staticmethod
    def _scope(row: sqlite3.Row) -> AnalysisScope:
        return AnalysisScope(row["id"], row["name"], row["created_at"], row["retired_at"])

    @staticmethod
    def _member(row: sqlite3.Row) -> RepositoryMember:
        values = dict(row)
        values["declared_aliases"] = tuple(json.loads(values.pop("declared_aliases_json", "[]")))
        return RepositoryMember(**values)

    @staticmethod
    def _attempt(row: sqlite3.Row) -> RepositoryAttempt:
        return RepositoryAttempt(
            id=row["id"], run_id=row["run_id"], member_id=row["member_id"],
            attempt_no=row["attempt_no"], status=AttemptStatus(row["status"]),
            stage=AttemptStage(row["stage"]), requested_at=row["requested_at"],
            started_at=row["started_at"], completed_at=row["completed_at"],
            retry_of_attempt_id=row["retry_of_attempt_id"], snapshot_id=row["snapshot_id"],
            cancellation_requested_at=row["cancellation_requested_at"],
            worker_instance_id=row["worker_instance_id"], progress=json.loads(row["progress_json"]),
            error_code=row["error_code"], error_message=row["error_message"],
            error_details=json.loads(row["error_details_json"]), log_excerpt=row["log_excerpt"],
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> RepositoryAnalysisSnapshot:
        return RepositoryAnalysisSnapshot(
            id=row["id"], member_id=row["member_id"], evidence_digest=row["evidence_digest"],
            captured_at=row["captured_at"], observed_branch=row["observed_branch"],
            observed_head_commit=row["observed_head_commit"], dirty=bool(row["dirty"]),
            codegraph_version=row["codegraph_version"],
            codegraph_schema_digest=row["codegraph_schema_digest"],
            analyzer_bundle_digest=row["analyzer_bundle_digest"], rules_digest=row["rules_digest"],
            report_schema_version=row["report_schema_version"], options_digest=row["options_digest"],
            facts_digest=row["facts_digest"], facts_storage=row["facts_storage"],
            facts=json.loads(row["facts_json"]) if row["facts_json"] is not None else None,
            facts_artifact_key=row["facts_artifact_key"],
            analysis_completeness=row["analysis_completeness"], orphaned=bool(row["orphaned"]),
            deletion_state=row["deletion_state"], label=row["label"], note=row["note"],
            pinned=bool(row["pinned"]), metadata_version=row["metadata_version"],
        )

    @staticmethod
    def _view(row: sqlite3.Row, members: Sequence[sqlite3.Row]) -> GraphView:
        return GraphView(
            id=row["id"], digest=row["digest"], lifecycle=ViewLifecycle(row["lifecycle"]),
            completeness=row["completeness"], created_at=row["created_at"],
            last_accessed_at=row["last_accessed_at"], expires_at=row["expires_at"],
            selector=json.loads(row["selector_json"]), label=row["label"], note=row["note"],
            external_context=json.loads(row["external_context"]),
            tags=tuple(json.loads(row["tags_json"])),
            members=tuple(
                GraphViewMember(
                    member_id=item["member_id"], ordinal=item["ordinal"],
                    snapshot_id=item["snapshot_id"],
                    availability=ViewAvailability(item["availability"]),
                    display_name=item["display_name"],
                    declared_aliases=tuple(json.loads(item["declared_aliases_json"])),
                )
                for item in members
            ),
            scope_id=row["scope_id"],
            topology_rules_digest=row["topology_rules_digest"],
            identity_schema=row["identity_schema"],
        )

    @staticmethod
    def _run(row: sqlite3.Row, members: Sequence[sqlite3.Row]) -> AnalysisRun:
        return AnalysisRun(
            id=row["id"], status=RunStatus(row["status"]), requested_at=row["requested_at"],
            started_at=row["started_at"], completed_at=row["completed_at"],
            external_context=json.loads(row["external_context"]), tags=tuple(json.loads(row["tags_json"])),
            idempotency_key=row["idempotency_key"],
            members=tuple(
                AnalysisRunMember(
                    member_id=item["member_id"], ordinal=item["ordinal"],
                    disposition=RunMemberDisposition(item["disposition"]),
                    attempt_id=item["attempt_id"], blocking_attempt_id=item["blocking_attempt_id"],
                )
                for item in members
            ),
        )


# Concise compatibility name for application-layer composition.
AnalysisSnapshotStore = AnalysisSnapshotSQLiteStore

# Backward-friendly spelling retained while callers move to the conventional
# ``Error`` suffix.
AttemptStateConflict = AttemptStateConflictError
