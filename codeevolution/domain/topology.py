"""Immutable contracts and canonical identity helpers for service topology.

The topology pipeline deliberately keeps these types independent from HTTP,
SQLite and CodeGraph adapters.  They are shared by repository collectors,
Graph View resolution and artifact builders, so their serialized shape is part
of the persisted artifact contract.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

TOPOLOGY_ARTIFACT_SCHEMA_VERSION = "topology-artifact/v1"
GRAPH_ARTIFACT_KEY_SCHEMA_VERSION = "graph-artifact-key/v1"
TOPOLOGY_NORMALIZATION_SCHEMA_VERSION = "topology-normalization/v1"


def _json_value(value: Any) -> Any:
    """Return a JSON-only, deterministic representation of a domain value."""
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"not canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize without whitespace and with stable Unicode/key ordering."""
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


def _clean_relative_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    if not value or value.startswith("/") or value.startswith("../") or "/../" in value:
        raise ValueError("location file must be a safe relative path")
    normalized = posixpath.normpath(value)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise ValueError("location file must be a safe relative path")
    return normalized


def canonical_authority(value: str, *, scheme: str | None = None) -> str:
    """Normalize a service authority and reject credentials/query fragments.

    A caller may pass a bare ``host[:port]`` or an absolute URL.  This helper
    deliberately returns only an authority; paths are normalized separately.
    """
    raw = value.strip()
    parsed = urlsplit(raw if "://" in raw else f"//{raw}", scheme=scheme or "")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("authority must not contain credentials, query, or fragment")
    if not parsed.hostname:
        raise ValueError("authority must include a host")
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    effective_scheme = (parsed.scheme or scheme or "").lower()
    if port is None or (effective_scheme == "http" and port == 80) or (effective_scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


_PARAM_SEGMENT = re.compile(r"^(?:\{[^{}]+\}|:[^/]+|<[^<>]+>)$")


def canonical_path_template(value: str) -> str:
    """Canonicalize route templates while retaining static route semantics."""
    raw = value.strip()
    if not raw:
        return "/"
    parsed = urlsplit(raw)
    if parsed.query or parsed.fragment:
        raise ValueError("path template must not contain query or fragment")
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.split("/") if part]
    normalized = ["{param}" if _PARAM_SEGMENT.match(part) else part for part in parts]
    return "/" + "/".join(normalized) if normalized else "/"


def canonical_aliases(values: Sequence[str]) -> tuple[str, ...]:
    """Freeze exact declared aliases in stable order, rejecting empty entries."""
    aliases = {value.strip() for value in values}
    if "" in aliases:
        raise ValueError("declared aliases must not be empty")
    return tuple(sorted(aliases))


@dataclass(frozen=True)
class Location:
    file: str
    start_line: int
    start_column: int | None = None
    end_line: int | None = None
    end_column: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "file", _clean_relative_path(self.file))
        if self.start_line < 1 or (self.end_line is not None and self.end_line < self.start_line):
            raise ValueError("location lines must be positive and ordered")


@dataclass(frozen=True, order=True)
class EntryRef:
    member_id: str
    snapshot_id: str
    entry_id: str


@dataclass(frozen=True)
class SnapshotHandle:
    member_id: str
    snapshot_id: str
    display_name: str
    declared_aliases: tuple[str, ...]
    facts_digest: str
    communication_artifact_key: str
    completeness: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared_aliases", canonical_aliases(self.declared_aliases))


@dataclass(frozen=True)
class UnavailableMember:
    member_id: str
    display_name: str
    availability: str
    reason: str = ""


@dataclass(frozen=True)
class ResolvedGraphView:
    scope_id: str
    view_id: str
    view_digest: str
    members: tuple[SnapshotHandle | UnavailableMember, ...]


@dataclass(frozen=True)
class ConfidenceComponent:
    name: str
    score: float
    rule: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("confidence component score must be in [0, 1]")


@dataclass(frozen=True)
class Confidence:
    score: float
    level: str
    components: tuple[ConfidenceComponent, ...] = ()
    caps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("confidence score must be in [0, 1]")
        expected = "high" if self.score >= 0.9 else "medium" if self.score >= 0.8 else "low"
        if self.level != expected:
            raise ValueError("confidence level does not match score")


def stable_edge_id(kind: str, body: Mapping[str, Any]) -> str:
    """Return an edge id without allowing a pre-existing id to self-reference."""
    if not kind:
        raise ValueError("edge kind is required")
    identity = {key: value for key, value in body.items() if key != "edge_id"}
    return f"{kind}:" + canonical_digest({"schema": TOPOLOGY_NORMALIZATION_SCHEMA_VERSION, "body": identity})


@dataclass(frozen=True)
class TopologyArtifactRequestSpec:
    """The immutable audit and cache identity for a topology generation job."""

    view_digest: str
    scope_id: str
    members: tuple[tuple[str, str | None, str], ...]
    analyzer_bundle_digest: str
    rules_digest: str
    normalized_params: Mapping[str, Any]
    artifact_kind: str = "topology"
    artifact_schema_version: str = TOPOLOGY_ARTIFACT_SCHEMA_VERSION
    identity_schema: str = GRAPH_ARTIFACT_KEY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.artifact_kind != "topology":
            raise ValueError("only topology uses the topology artifact request spec")
        if dict(self.normalized_params):
            raise ValueError("topology artifact parameters must be empty")
        members = tuple(sorted(self.members, key=lambda item: item[0]))
        if not members or len({item[0] for item in members}) != len(members):
            raise ValueError("topology request members must be non-empty and unique")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "normalized_params", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_schema": self.identity_schema,
            "view_digest": self.view_digest,
            "scope_id": self.scope_id,
            "members": [
                {"member_id": member_id, "snapshot_id": snapshot_id, "availability": availability}
                for member_id, snapshot_id, availability in self.members
            ],
            "artifact_kind": self.artifact_kind,
            "normalized_params": {},
            "analyzer_bundle_digest": self.analyzer_bundle_digest,
            "rules_digest": self.rules_digest,
            "artifact_schema_version": self.artifact_schema_version,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def cache_key(self) -> str:
        return self.digest
