"""Resolve a Graph View into immutable topology inputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from codeevolution.analysis.communication.schema import (
    RepositoryCommunicationArtifact,
    deserialize_communication_artifact,
)
from codeevolution.domain.analysis_snapshot import ViewAvailability
from codeevolution.domain.topology import ResolvedGraphView, SnapshotHandle, UnavailableMember
from codeevolution.infrastructure.analysis_snapshot_sqlite import ViewExpiredError


class GraphViewResolutionError(RuntimeError):
    """Raised when a frozen View cannot be used for topology analysis."""


class GraphViewResolver:
    """Turn persisted Graph View membership into a topology-safe value object.

    ``communication_loader`` may return a parsed artifact, canonical JSON, or
    ``None``.  It is intentionally injected so the resolver never needs a
    repository path or a live CodeGraph database.
    """

    def __init__(self, store, communication_loader: Callable[[Any], Any] | None = None):
        self.store = store
        self.communication_loader = communication_loader

    def resolve(self, view_id: str) -> tuple[ResolvedGraphView, dict[str, RepositoryCommunicationArtifact]]:
        try:
            view = self.store.get_view(view_id)
        except ViewExpiredError as error:
            raise GraphViewResolutionError("view_expired") from error
        if view is None:
            raise KeyError(view_id)
        if view.scope_id is None:
            raise GraphViewResolutionError("legacy_mixed_scope_view")

        members: list[SnapshotHandle | UnavailableMember] = []
        artifacts: dict[str, RepositoryCommunicationArtifact] = {}
        for view_member in view.members:
            if view_member.availability != ViewAvailability.AVAILABLE or not view_member.snapshot_id:
                members.append(
                    UnavailableMember(
                        view_member.member_id,
                        view_member.display_name or view_member.member_id,
                        view_member.availability.value,
                        "snapshot_not_available",
                    )
                )
                continue
            snapshot = self.store.get_snapshot(view_member.snapshot_id)
            if snapshot is None or snapshot.deletion_state != "active":
                raise GraphViewResolutionError("snapshot_unavailable")
            summary, artifact_key = self._communication_metadata(snapshot)
            if not artifact_key:
                # Old, valid Snapshots have no communication artifact.  They
                # participate in a partial View but cannot produce edges.
                members.append(
                    UnavailableMember(
                        view_member.member_id,
                        view_member.display_name or view_member.member_id,
                        "unparsed",
                        "communication_facts_unavailable",
                    )
                )
                continue
            artifact = self._load(snapshot, artifact_key)
            if artifact is None:
                raise GraphViewResolutionError("snapshot_artifact_corrupt")
            if artifact.snapshot_id != snapshot.id:
                raise GraphViewResolutionError("snapshot_artifact_corrupt")
            artifacts[snapshot.id] = artifact
            members.append(
                SnapshotHandle(
                    view_member.member_id,
                    snapshot.id,
                    view_member.display_name or view_member.member_id,
                    view_member.declared_aliases,
                    snapshot.facts_digest,
                    artifact_key,
                    artifact.completeness,
                )
            )
        resolved = ResolvedGraphView(view.scope_id, view.id, view.digest, tuple(members))
        return resolved, artifacts

    @staticmethod
    def _communication_metadata(snapshot) -> tuple[Mapping[str, Any], str]:
        facts = snapshot.facts if isinstance(snapshot.facts, Mapping) else {}
        summary = facts.get("communication_summary", {})
        if not isinstance(summary, Mapping):
            return {}, ""
        return summary, str(summary.get("artifact_key", ""))

    def _load(self, snapshot, artifact_key: str) -> RepositoryCommunicationArtifact | None:
        if self.communication_loader is None:
            return None
        raw = self.communication_loader(snapshot)
        if raw is None:
            return None
        if isinstance(raw, RepositoryCommunicationArtifact):
            return raw
        if isinstance(raw, bytes | str):
            try:
                return deserialize_communication_artifact(raw)
            except ValueError as error:
                raise GraphViewResolutionError("snapshot_artifact_corrupt") from error
        if isinstance(raw, Mapping):
            try:
                return RepositoryCommunicationArtifact.from_dict(raw)
            except ValueError as error:
                raise GraphViewResolutionError("snapshot_artifact_corrupt") from error
        raise GraphViewResolutionError("snapshot_artifact_corrupt")


__all__ = ["GraphViewResolutionError", "GraphViewResolver"]
