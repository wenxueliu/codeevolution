"""Domain DTOs for endpoint-scoped API explanation snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExplanationSnapshot:
    id: str
    repo_name: str
    api_key: str
    method: str
    path: str
    handler: str
    entry_node_key: str
    source_revision: str
    source_digest: str
    graph_digest: str
    model_id: str
    prompt_version: str
    schema_version: str
    status: str = "pending"
    member_name: str = ""
    explanation: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    statistics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: int = 0
    completed_at: int | None = None
    repository_snapshot_id: str = ""
    prompt_profile_id: str = ""
    prompt_digest: str = ""


@dataclass(frozen=True)
class ExplanationNode:
    snapshot_id: str
    node_key: str
    source_hash: str
    local_digest: str
    aggregate_digest: str
    local_explanation: dict[str, Any] = field(default_factory=dict)
    aggregate_explanation: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"


@dataclass(frozen=True)
class ExplanationChunk:
    snapshot_id: str
    node_key: str
    chunk_index: int
    line_start: int
    line_end: int
    source_hash: str
    explanation: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"


@dataclass(frozen=True)
class ExplanationEdge:
    snapshot_id: str
    caller_key: str
    callee_key: str
    call_site: dict[str, Any] = field(default_factory=dict)
    call_context: dict[str, Any] = field(default_factory=dict)
