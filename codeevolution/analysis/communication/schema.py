"""Schema and canonical serialization for repository communication artifacts.

This is the repository-side half of cross-service topology analysis.  It
contains observations only; matching an observation to another repository is a
Graph View concern and must happen later.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol
from unicodedata import normalize

from codeevolution.ports import CodeGraphRepository, SourceProvider

REPOSITORY_COMMUNICATION_SCHEMA = "repository-communication/v1"
TOPOLOGY_NORMALIZATION_SCHEMA = "topology-normalization/v1"


class CommunicationSchemaError(ValueError):
    """Raised for non-canonical or unsafe communication evidence."""


class CollectorStatus(str, Enum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


def _require_nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommunicationSchemaError(f"{field_name} must be a non-empty string")
    return value


def _freeze_json(value: Any, *, field_name: str = "value") -> Any:
    """Copy a JSON value into an immutable, JSON-compatible representation."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CommunicationSchemaError(f"{field_name} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise CommunicationSchemaError(f"{field_name} object keys must be strings")
            copied[key] = _freeze_json(nested, field_name=f"{field_name}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, field_name=field_name) for item in value)
    raise CommunicationSchemaError(f"{field_name} is not JSON compatible")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class Location:
    """A safe source location whose path is relative to the evidence manifest."""

    file: str
    start_line: int
    start_column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.file, "location.file")
        if self.file != normalize("NFC", self.file):
            raise CommunicationSchemaError("location.file must be NFC normalized")
        path = PurePosixPath(self.file)
        if path.is_absolute() or self.file.startswith("/") or "\\" in self.file:
            raise CommunicationSchemaError("location.file must be a relative POSIX path")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise CommunicationSchemaError("location.file must not contain dot segments")
        if not isinstance(self.start_line, int) or self.start_line < 1:
            raise CommunicationSchemaError("location.start_line must be >= 1")
        if self.end_line is not None and (
            not isinstance(self.end_line, int) or self.end_line < self.start_line
        ):
            raise CommunicationSchemaError("location.end_line must be >= start_line")
        for name, value in (("start_column", self.start_column), ("end_column", self.end_column)):
            if value is not None and (not isinstance(value, int) or value < 1):
                raise CommunicationSchemaError(f"location.{name} must be >= 1 or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Location":
        return cls(
            file=value.get("file", ""),
            start_line=value.get("start_line"),
            start_column=value.get("start_column"),
            end_line=value.get("end_line"),
            end_column=value.get("end_column"),
        )


@dataclass(frozen=True)
class NodeRef:
    """A node identity meaningful only together with its snapshot ID."""

    snapshot_id: str
    node_id: str
    kind: str
    name: str
    qualified_name: str
    location: Location | None = None

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "node_id", "kind", "name", "qualified_name"):
            _require_nonempty(getattr(self, field_name), f"node_ref.{field_name}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
        }
        if self.location is not None:
            result["location"] = self.location.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NodeRef":
        location = value.get("location")
        return cls(
            snapshot_id=value.get("snapshot_id", ""),
            node_id=value.get("node_id", ""),
            kind=value.get("kind", ""),
            name=value.get("name", ""),
            qualified_name=value.get("qualified_name", ""),
            location=Location.from_dict(location) if isinstance(location, Mapping) else None,
        )


@dataclass(frozen=True)
class EntryFact:
    """A production entry point discovered within one repository snapshot."""

    entry_id: str
    kind: str
    protocol: str
    handler: NodeRef
    method: str | None = None
    path_template: str | None = None
    channel: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("entry_id", "kind", "protocol"):
            _require_nonempty(getattr(self, field_name), f"entry.{field_name}")
        object.__setattr__(
            self, "evidence", _freeze_json(self.evidence, field_name="entry.evidence")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "protocol": self.protocol,
            "method": self.method,
            "path_template": self.path_template,
            "channel": self.channel,
            "handler": self.handler.to_dict(),
            "evidence": _json_value(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EntryFact":
        handler = value.get("handler")
        if not isinstance(handler, Mapping):
            raise CommunicationSchemaError("entry.handler must be an object")
        return cls(
            entry_id=value.get("entry_id", ""),
            kind=value.get("kind", ""),
            protocol=value.get("protocol", ""),
            method=value.get("method"),
            path_template=value.get("path_template"),
            channel=value.get("channel"),
            handler=NodeRef.from_dict(handler),
            evidence=value.get("evidence", {}),
        )


@dataclass(frozen=True)
class CommunicationObservation:
    """A target-independent callsite observation with protocol-specific payload."""

    observation_id: str
    entry_id: str | None
    caller: NodeRef | None
    callsite: Location | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    extraction_confidence: float | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.observation_id, "observation.observation_id")
        if self.entry_id is not None:
            _require_nonempty(self.entry_id, "observation.entry_id")
        if self.extraction_confidence is not None and not 0 <= self.extraction_confidence <= 1:
            raise CommunicationSchemaError(
                "observation.extraction_confidence must be between 0 and 1"
            )
        object.__setattr__(
            self, "payload", _freeze_json(self.payload, field_name="observation.payload")
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "observation_id": self.observation_id,
            "entry_id": self.entry_id,
            "payload": _json_value(self.payload),
            "extraction_confidence": self.extraction_confidence,
        }
        if self.caller is not None:
            result["caller"] = self.caller.to_dict()
        if self.callsite is not None:
            result["callsite"] = self.callsite.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommunicationObservation":
        caller = value.get("caller")
        callsite = value.get("callsite")
        return cls(
            observation_id=value.get("observation_id", ""),
            entry_id=value.get("entry_id"),
            caller=NodeRef.from_dict(caller) if isinstance(caller, Mapping) else None,
            callsite=Location.from_dict(callsite) if isinstance(callsite, Mapping) else None,
            payload=value.get("payload", {}),
            extraction_confidence=value.get("extraction_confidence"),
        )


@dataclass(frozen=True)
class CollectorCoverage:
    collector: str
    rule_version: str
    status: CollectorStatus
    reason: str | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)
    truncated_count: int = 0
    required: bool = True

    def __post_init__(self) -> None:
        _require_nonempty(self.collector, "coverage.collector")
        _require_nonempty(self.rule_version, "coverage.rule_version")
        if self.truncated_count < 0:
            raise CommunicationSchemaError("coverage.truncated_count must not be negative")
        object.__setattr__(self, "status", CollectorStatus(self.status))
        object.__setattr__(
            self, "coverage", _freeze_json(self.coverage, field_name="coverage.coverage")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector": self.collector,
            "rule_version": self.rule_version,
            "status": self.status.value,
            "reason": self.reason,
            "coverage": _json_value(self.coverage),
            "truncated_count": self.truncated_count,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CollectorCoverage":
        return cls(
            collector=value.get("collector", ""),
            rule_version=value.get("rule_version", ""),
            status=value.get("status", ""),
            reason=value.get("reason"),
            coverage=value.get("coverage", {}),
            truncated_count=value.get("truncated_count", 0),
            required=value.get("required", True),
        )


@dataclass(frozen=True)
class CollectorRuleSet:
    """Frozen, versioned collector rules; never a repository path or callback."""

    digest: str
    version: str
    budgets: Mapping[str, int] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty(self.digest, "rules.digest")
        _require_nonempty(self.version, "rules.version")
        object.__setattr__(self, "budgets", _freeze_json(self.budgets, field_name="rules.budgets"))
        object.__setattr__(self, "options", _freeze_json(self.options, field_name="rules.options"))


@dataclass(frozen=True)
class CollectorResult:
    """One collector's result, ready to merge into an immutable artifact."""

    coverage: CollectorCoverage
    entries: tuple[EntryFact, ...] = ()
    http_outbounds: tuple[CommunicationObservation, ...] = ()
    message_publications: tuple[CommunicationObservation, ...] = ()
    message_subscriptions: tuple[CommunicationObservation, ...] = ()
    grpc_clients: tuple[CommunicationObservation, ...] = ()
    grpc_servers: tuple[CommunicationObservation, ...] = ()
    resource_accesses: tuple[CommunicationObservation, ...] = ()
    unresolved_observations: tuple[CommunicationObservation, ...] = ()


class CommunicationCollector(Protocol):
    """Collect target-independent facts from immutable snapshot inputs only."""

    def collect(
        self,
        graph: CodeGraphRepository,
        sources: SourceProvider,
        rules: CollectorRuleSet,
    ) -> CollectorResult: ...


@dataclass(frozen=True)
class CommunicationArtifactReference:
    artifact_key: str
    payload_digest: str
    byte_size: int

    def __post_init__(self) -> None:
        _require_nonempty(self.artifact_key, "communication artifact_key")
        _require_nonempty(self.payload_digest, "communication payload_digest")
        if self.byte_size < 0:
            raise CommunicationSchemaError("communication byte_size must not be negative")


@dataclass(frozen=True)
class RepositoryCommunicationArtifact:
    """Canonical payload written as ``repository-communication/v1``."""

    snapshot_id: str
    rules_digest: str
    entries: tuple[EntryFact, ...] = ()
    http_outbounds: tuple[CommunicationObservation, ...] = ()
    message_publications: tuple[CommunicationObservation, ...] = ()
    message_subscriptions: tuple[CommunicationObservation, ...] = ()
    grpc_clients: tuple[CommunicationObservation, ...] = ()
    grpc_servers: tuple[CommunicationObservation, ...] = ()
    resource_accesses: tuple[CommunicationObservation, ...] = ()
    unresolved_observations: tuple[CommunicationObservation, ...] = ()
    collector_coverage: tuple[CollectorCoverage, ...] = ()
    schema: str = REPOSITORY_COMMUNICATION_SCHEMA
    normalization_schema: str = TOPOLOGY_NORMALIZATION_SCHEMA

    def __post_init__(self) -> None:
        _require_nonempty(self.snapshot_id, "artifact.snapshot_id")
        _require_nonempty(self.rules_digest, "artifact.rules_digest")
        if self.schema != REPOSITORY_COMMUNICATION_SCHEMA:
            raise CommunicationSchemaError(f"unsupported communication schema: {self.schema}")
        if self.normalization_schema != TOPOLOGY_NORMALIZATION_SCHEMA:
            raise CommunicationSchemaError(
                f"unsupported normalization schema: {self.normalization_schema}"
            )
        _validate_unique("entry_id", self.entries, lambda item: item.entry_id)
        all_observations = self._all_observations()
        _validate_unique("observation_id", all_observations, lambda item: item.observation_id)
        entry_ids = {item.entry_id for item in self.entries}
        for observation in all_observations:
            if observation.entry_id is not None and observation.entry_id not in entry_ids:
                raise CommunicationSchemaError(
                    f"observation {observation.observation_id} references unknown entry {observation.entry_id}"
                )
        _validate_unique("collector", self.collector_coverage, lambda item: item.collector)

    def _all_observations(self) -> tuple[CommunicationObservation, ...]:
        return (
            self.http_outbounds
            + self.message_publications
            + self.message_subscriptions
            + self.grpc_clients
            + self.grpc_servers
            + self.resource_accesses
            + self.unresolved_observations
        )

    @property
    def completeness(self) -> str:
        coverage = self.collector_coverage
        if any(item.required and item.status is CollectorStatus.UNAVAILABLE for item in coverage):
            return "unavailable"
        if any(item.status is not CollectorStatus.COMPLETE for item in coverage):
            return "partial"
        return "complete"

    @property
    def observation_counts(self) -> dict[str, int]:
        return {
            "entries": len(self.entries),
            "http_outbounds": len(self.http_outbounds),
            "message_publications": len(self.message_publications),
            "message_subscriptions": len(self.message_subscriptions),
            "grpc_clients": len(self.grpc_clients),
            "grpc_servers": len(self.grpc_servers),
            "resource_accesses": len(self.resource_accesses),
            "unresolved_observations": len(self.unresolved_observations),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "normalization_schema": self.normalization_schema,
            "snapshot_id": self.snapshot_id,
            "rules_digest": self.rules_digest,
            "completeness": self.completeness,
            "entries": [
                item.to_dict() for item in sorted(self.entries, key=lambda item: item.entry_id)
            ],
            "http_outbounds": _observation_dicts(self.http_outbounds),
            "message_publications": _observation_dicts(self.message_publications),
            "message_subscriptions": _observation_dicts(self.message_subscriptions),
            "grpc_clients": _observation_dicts(self.grpc_clients),
            "grpc_servers": _observation_dicts(self.grpc_servers),
            "resource_accesses": _observation_dicts(self.resource_accesses),
            "unresolved_observations": _observation_dicts(self.unresolved_observations),
            "collector_coverage": [
                item.to_dict()
                for item in sorted(self.collector_coverage, key=lambda item: item.collector)
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RepositoryCommunicationArtifact":
        if not isinstance(value, Mapping):
            raise CommunicationSchemaError("communication artifact must be an object")
        return cls(
            schema=value.get("schema", ""),
            normalization_schema=value.get("normalization_schema", ""),
            snapshot_id=value.get("snapshot_id", ""),
            rules_digest=value.get("rules_digest", ""),
            entries=_decode_entries(value, "entries"),
            http_outbounds=_decode_observations(value, "http_outbounds"),
            message_publications=_decode_observations(value, "message_publications"),
            message_subscriptions=_decode_observations(value, "message_subscriptions"),
            grpc_clients=_decode_observations(value, "grpc_clients"),
            grpc_servers=_decode_observations(value, "grpc_servers"),
            resource_accesses=_decode_observations(value, "resource_accesses"),
            unresolved_observations=_decode_observations(value, "unresolved_observations"),
            collector_coverage=_decode_coverage(value),
        )

    def payload_digest(self) -> str:
        return "sha256:" + hashlib.sha256(serialize_communication_artifact(self)).hexdigest()

    def summary(self, reference: CommunicationArtifactReference) -> dict[str, Any]:
        expected_digest = self.payload_digest()
        if reference.payload_digest != expected_digest:
            raise CommunicationSchemaError(
                "communication artifact reference digest does not match payload"
            )
        return {
            "schema_version": self.schema,
            "normalization_schema": self.normalization_schema,
            "artifact_key": reference.artifact_key,
            "payload_digest": reference.payload_digest,
            "byte_size": reference.byte_size,
            "completeness": self.completeness,
            "observation_counts": self.observation_counts,
        }


def build_communication_artifact(
    *, snapshot_id: str, rules_digest: str, results: Sequence[CollectorResult]
) -> RepositoryCommunicationArtifact:
    """Merge collector results without making cross-repository assumptions."""

    return RepositoryCommunicationArtifact(
        snapshot_id=snapshot_id,
        rules_digest=rules_digest,
        entries=tuple(item for result in results for item in result.entries),
        http_outbounds=tuple(item for result in results for item in result.http_outbounds),
        message_publications=tuple(
            item for result in results for item in result.message_publications
        ),
        message_subscriptions=tuple(
            item for result in results for item in result.message_subscriptions
        ),
        grpc_clients=tuple(item for result in results for item in result.grpc_clients),
        grpc_servers=tuple(item for result in results for item in result.grpc_servers),
        resource_accesses=tuple(item for result in results for item in result.resource_accesses),
        unresolved_observations=tuple(
            item for result in results for item in result.unresolved_observations
        ),
        collector_coverage=tuple(result.coverage for result in results),
    )


def serialize_communication_artifact(artifact: RepositoryCommunicationArtifact) -> bytes:
    """Return canonical UTF-8 JSON suitable for content-addressed storage."""

    return _canonical_json(artifact.to_dict())


def deserialize_communication_artifact(payload: bytes | str) -> RepositoryCommunicationArtifact:
    """Parse and validate a canonical communication payload."""

    try:
        decoded = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CommunicationSchemaError("communication artifact is not valid JSON") from error
    artifact = RepositoryCommunicationArtifact.from_dict(decoded)
    if serialize_communication_artifact(artifact) != (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    ):
        raise CommunicationSchemaError("communication artifact JSON is not canonical")
    return artifact


def _validate_unique(label: str, values: Sequence[Any], key) -> None:
    seen: set[str] = set()
    for value in values:
        identity = key(value)
        if identity in seen:
            raise CommunicationSchemaError(f"duplicate {label}: {identity}")
        seen.add(identity)


def _observation_dicts(values: Sequence[CommunicationObservation]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in sorted(values, key=lambda item: item.observation_id)]


def _require_array(value: Mapping[str, Any], field_name: str) -> Sequence[Any]:
    raw = value.get(field_name, [])
    if not isinstance(raw, list):
        raise CommunicationSchemaError(f"{field_name} must be an array")
    return raw


def _decode_entries(value: Mapping[str, Any], field_name: str) -> tuple[EntryFact, ...]:
    result: list[EntryFact] = []
    for item in _require_array(value, field_name):
        if not isinstance(item, Mapping):
            raise CommunicationSchemaError(f"{field_name} must contain objects")
        result.append(EntryFact.from_dict(item))
    return tuple(result)


def _decode_observations(
    value: Mapping[str, Any], field_name: str
) -> tuple[CommunicationObservation, ...]:
    result: list[CommunicationObservation] = []
    for item in _require_array(value, field_name):
        if not isinstance(item, Mapping):
            raise CommunicationSchemaError(f"{field_name} must contain objects")
        result.append(CommunicationObservation.from_dict(item))
    return tuple(result)


def _decode_coverage(value: Mapping[str, Any]) -> tuple[CollectorCoverage, ...]:
    result: list[CollectorCoverage] = []
    for item in _require_array(value, "collector_coverage"):
        if not isinstance(item, Mapping):
            raise CommunicationSchemaError("collector_coverage must contain objects")
        result.append(CollectorCoverage.from_dict(item))
    return tuple(result)
