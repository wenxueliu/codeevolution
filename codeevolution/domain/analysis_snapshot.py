"""Domain types and lifecycle rules for repository analysis snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AttemptStage(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    DIGEST_BEFORE = "digest_before"
    CODEGRAPH_INIT = "codegraph_init"
    CODEGRAPH_SYNC = "codegraph_sync"
    DIGEST_AFTER = "digest_after"
    FREEZING_GRAPH = "freezing_graph"
    FREEZING_SOURCES = "freezing_sources"
    DIGEST_FINAL = "digest_final"
    ANALYZING = "analyzing"
    PUBLISHING = "publishing"
    FINISHED = "finished"


class RunMemberDisposition(str, Enum):
    QUEUED = "queued"
    ALREADY_RUNNING = "already_running"


class ViewLifecycle(str, Enum):
    EPHEMERAL = "ephemeral"
    PINNED = "pinned"


class ViewAvailability(str, Enum):
    AVAILABLE = "available"
    UNPARSED = "unparsed"
    RETIRED = "retired"


TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.COMPLETED,
        AttemptStatus.UNCHANGED,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
        AttemptStatus.INTERRUPTED,
    }
)
TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)


@dataclass(frozen=True)
class AnalysisScope:
    id: str
    name: str
    created_at: str
    retired_at: str | None = None


@dataclass(frozen=True)
class RepositoryMember:
    id: str
    scope_id: str
    display_name: str
    registered_path: str
    path_identity: str
    created_at: str
    updated_at: str
    retired_at: str | None = None
    declared_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryAttempt:
    id: str
    run_id: str
    member_id: str
    attempt_no: int
    status: AttemptStatus
    stage: AttemptStage
    requested_at: str
    started_at: str | None = None
    completed_at: str | None = None
    retry_of_attempt_id: str | None = None
    snapshot_id: str | None = None
    cancellation_requested_at: str | None = None
    worker_instance_id: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    log_excerpt: str | None = None


@dataclass(frozen=True)
class AnalysisRunMember:
    member_id: str
    ordinal: int
    disposition: RunMemberDisposition
    attempt_id: str | None = None
    blocking_attempt_id: str | None = None


@dataclass(frozen=True)
class AnalysisRun:
    id: str
    status: RunStatus
    requested_at: str
    started_at: str | None = None
    completed_at: str | None = None
    external_context: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    idempotency_key: str | None = None
    members: tuple[AnalysisRunMember, ...] = ()


@dataclass(frozen=True)
class EvidenceBundle:
    digest: str
    artifact_key: str
    manifest_schema_version: str
    source_digest: str
    graph_digest: str
    graph_blob_sha256: str
    byte_size: int
    capture_policy_digest: str
    capture_completeness: str
    created_at: str = ""
    deletion_state: str = "active"


@dataclass(frozen=True)
class RepositoryAnalysisSnapshot:
    id: str
    member_id: str
    evidence_digest: str
    captured_at: str
    dirty: bool
    codegraph_version: str
    codegraph_schema_digest: str
    analyzer_bundle_digest: str
    rules_digest: str
    report_schema_version: str
    options_digest: str
    facts_digest: str
    facts_storage: str
    analysis_completeness: str
    observed_branch: str | None = None
    observed_head_commit: str | None = None
    facts: dict[str, Any] | None = None
    facts_artifact_key: str | None = None
    orphaned: bool = False
    deletion_state: str = "active"
    label: str = ""
    note: str = ""
    pinned: bool = False
    metadata_version: int = 1


@dataclass(frozen=True)
class GraphViewMember:
    member_id: str
    ordinal: int
    snapshot_id: str | None
    availability: ViewAvailability
    display_name: str = ""
    declared_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphView:
    id: str
    digest: str
    lifecycle: ViewLifecycle
    completeness: str
    created_at: str
    last_accessed_at: str
    expires_at: str | None
    selector: dict[str, Any]
    label: str = ""
    note: str = ""
    external_context: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    members: tuple[GraphViewMember, ...] = ()
    scope_id: str | None = None
    topology_rules_digest: str = ""
    identity_schema: str = "graph-view/v2"


def aggregate_run_status(
    outcomes: Iterable[AttemptStatus | str | None], *, any_started: bool = False
) -> RunStatus:
    """Implement the complete run aggregation table from the design.

    ``None`` represents an ``already_running`` member (the effective ``blocked``
    outcome), not an unknown attempt.
    """

    values = [AttemptStatus(item) if item is not None else None for item in outcomes]
    if not values:
        raise ValueError("a run must contain at least one member")
    if AttemptStatus.RUNNING in values:
        return RunStatus.RUNNING
    if AttemptStatus.PENDING in values:
        return RunStatus.RUNNING if any_started else RunStatus.PENDING

    successes = {AttemptStatus.COMPLETED, AttemptStatus.UNCHANGED}
    if all(item in successes for item in values):
        return RunStatus.COMPLETED
    if any(item in successes for item in values):
        return RunStatus.PARTIAL
    if AttemptStatus.FAILED in values:
        return RunStatus.FAILED
    if AttemptStatus.INTERRUPTED in values:
        return RunStatus.INTERRUPTED
    if all(item == AttemptStatus.CANCELLED for item in values):
        return RunStatus.CANCELLED
    return RunStatus.FAILED
