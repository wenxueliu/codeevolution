"""Build a deterministic cross-service topology from frozen snapshot facts.

The builder is intentionally free of repository paths, live CodeGraph reads and
HTTP delivery concerns.  Its only inputs are a resolved Graph View and the
immutable communication artifacts belonging to the available members in that
View.  This makes a topology reproducible after a checkout is deleted.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from codeevolution.analysis.communication.schema import (
    CommunicationObservation,
    RepositoryCommunicationArtifact,
)
from codeevolution.analysis.topology.http_matcher import (
    HttpAlias,
    HttpInboundEndpoint,
    HttpObservation,
    HttpTopologyMatcher,
)
from codeevolution.domain.topology import (
    ResolvedGraphView,
    SnapshotHandle,
    UnavailableMember,
    canonical_digest,
    stable_edge_id,
)


class TopologyBuildError(RuntimeError):
    """Raised when a bound Snapshot cannot supply its immutable artifact."""


class TopologyArtifactBuilder:
    """Project repository communication facts into one Scope topology."""

    def __init__(self, matcher: HttpTopologyMatcher | None = None):
        self.http_matcher = matcher or HttpTopologyMatcher()

    def build(
        self,
        view: ResolvedGraphView,
        artifacts: Mapping[str, RepositoryCommunicationArtifact],
    ) -> dict[str, Any]:
        available = [member for member in view.members if isinstance(member, SnapshotHandle)]
        missing = [member for member in view.members if isinstance(member, UnavailableMember)]
        for member in available:
            artifact = artifacts.get(member.snapshot_id)
            if artifact is None:
                raise TopologyBuildError("snapshot_artifact_corrupt")
            if artifact.snapshot_id != member.snapshot_id:
                raise TopologyBuildError("snapshot_artifact_corrupt")

        endpoints = self._http_endpoints(available, artifacts)
        aliases = self._aliases(available)
        endpoint_dependencies: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        boundary_dependencies: list[dict[str, Any]] = []
        service_edges: dict[tuple[str, str, str], dict[str, Any]] = {}

        for member in available:
            artifact = artifacts[member.snapshot_id]
            for observation in artifact.http_outbounds:
                decision = self.http_matcher.match(
                    self._http_observation(member, observation), endpoints, aliases
                )
                source_entry_id = observation.entry_id or ""
                base = {
                    "observation_id": observation.observation_id,
                    "source": {
                        "member_id": member.member_id,
                        "snapshot_id": member.snapshot_id,
                        "entry_id": source_entry_id,
                    },
                    "transport": self._transport(observation),
                    "confidence": decision.confidence(),
                    "reasons": list(decision.reasons),
                    "evidence": self._evidence(observation),
                }
                if decision.is_confirmed and decision.target_member_id:
                    target_entry_ids = list(decision.target_entry_ids)
                    target = {
                        "member_id": decision.target_member_id,
                        "entry_ids": target_entry_ids,
                    }
                    dependency = {
                        **base,
                        "kind": "http",
                        "target": target,
                        "match_rule": self.http_matcher.rules.version,
                        "endpoint_alternatives": list(decision.endpoint_alternatives),
                    }
                    dependency["edge_id"] = stable_edge_id("endpoint", dependency)
                    endpoint_dependencies.append(dependency)
                    key = (member.member_id, decision.target_member_id, "http")
                    projection = service_edges.get(key)
                    if projection is None:
                        projection = {
                            "kind": "http",
                            "source_member_id": member.member_id,
                            "target_member_id": decision.target_member_id,
                            "supporting_endpoint_dependency_ids": [],
                        }
                        projection["edge_id"] = stable_edge_id("service", projection)
                        service_edges[key] = projection
                    projection["supporting_endpoint_dependency_ids"].append(
                        dependency["edge_id"]
                    )
                elif decision.status in {"candidate", "unresolved", "ambiguous"}:
                    candidates.append({**base, "kind": decision.status,
                                       "target_member_id": decision.target_member_id,
                                       "candidate_entry_ids": list(decision.candidates),
                                       "target_entry_ids": list(decision.target_entry_ids)})
                elif decision.status == "out_of_scope_or_unregistered":
                    boundary = {**base, "kind": decision.status}
                    boundary["boundary_id"] = stable_edge_id("boundary", boundary)
                    boundary_dependencies.append(boundary)

            self._append_message_dependencies(
                member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates
            )

        for projection in service_edges.values():
            projection["supporting_endpoint_dependency_ids"].sort()
        endpoint_dependencies.sort(key=lambda item: item["edge_id"])
        boundary_dependencies.sort(key=lambda item: item["boundary_id"])
        candidates.sort(key=lambda item: (item["kind"], item["observation_id"]))
        services = [
            self._service(member, artifacts.get(member.snapshot_id) if isinstance(member, SnapshotHandle) else None)
            for member in view.members
        ]
        coverage = self._coverage(view, artifacts)
        payload = {
            "schema_version": "topology-artifact/v1",
            "artifact_kind": "topology",
            "view_digest": view.view_digest,
            "scope_id": view.scope_id,
            "members": services,
            "services": services,
            "endpoint_dependencies": endpoint_dependencies,
            "service_projections": sorted(service_edges.values(), key=lambda item: item["edge_id"]),
            "message_alternatives": [],
            "resource_dependencies": self._resources(available, artifacts),
            "boundary_dependencies": boundary_dependencies,
            "candidates": candidates,
            "coverage": coverage,
            "warnings": ["partial_view_coverage"] if missing else [],
            "identity": {"view_digest": view.view_digest},
            "statistics": {
                "service_edges": len(service_edges),
                "endpoint_dependencies": len(endpoint_dependencies),
                "boundaries": len(boundary_dependencies),
                "candidates": len(candidates),
            },
        }
        payload["payload_digest"] = canonical_digest(payload)
        return payload

    @staticmethod
    def _http_endpoints(
        members: list[SnapshotHandle], artifacts: Mapping[str, RepositoryCommunicationArtifact]
    ) -> list[HttpInboundEndpoint]:
        result: list[HttpInboundEndpoint] = []
        for member in members:
            for entry in artifacts[member.snapshot_id].entries:
                if entry.protocol.lower() not in {"http", "https"} or not entry.method or not entry.path_template:
                    continue
                result.append(HttpInboundEndpoint(member.member_id, entry.entry_id, entry.method, entry.path_template))
        return sorted(result, key=lambda item: (item.member_id, item.entry_id))

    @staticmethod
    def _aliases(members: list[SnapshotHandle]) -> list[HttpAlias]:
        aliases: list[HttpAlias] = []
        for member in members:
            for value in member.declared_aliases:
                aliases.append(HttpAlias(member.member_id, value, source="declared", confidence=0.95))
        return sorted(aliases, key=lambda item: (item.authority, item.member_id))

    @staticmethod
    def _http_observation(member: SnapshotHandle, observation: CommunicationObservation) -> HttpObservation:
        payload = dict(observation.payload)
        request = payload.get("request", payload)
        if not isinstance(request, Mapping):
            request = {}
        binding = payload.get("client_binding", {})
        if not isinstance(binding, Mapping):
            binding = {}
        authority = request.get("authority") or payload.get("authority")
        return HttpObservation(
            observation_id=observation.observation_id,
            source_member_id=member.member_id,
            source_entry_id=observation.entry_id or "",
            method=request.get("method"),
            path=request.get("normalized_path", request.get("path")),
            authority=authority,
            client_binding_authority=binding.get("authority") or binding.get("base_authority"),
            entry_reachable=observation.entry_id is not None,
            extraction_confidence=observation.extraction_confidence or 1.0,
        )

    @staticmethod
    def _transport(observation: CommunicationObservation) -> dict[str, Any]:
        payload = observation.payload
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _evidence(observation: CommunicationObservation) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if observation.callsite:
            result["callsite"] = observation.callsite.to_dict()
        if observation.caller:
            result["caller"] = observation.caller.to_dict()
        return result

    @staticmethod
    def _service(member: SnapshotHandle | UnavailableMember, artifact: RepositoryCommunicationArtifact | None) -> dict[str, Any]:
        result = {
            "member_id": member.member_id,
            "display_name": member.display_name,
            "availability": "available" if isinstance(member, SnapshotHandle) else member.availability,
        }
        if isinstance(member, SnapshotHandle):
            result.update({"snapshot_id": member.snapshot_id, "completeness": artifact.completeness if artifact else "unavailable"})
            result["entries"] = [
                entry.to_dict()
                for entry in (artifact.entries if artifact else ())
            ]
        else:
            result["reason"] = member.reason
        return result

    @staticmethod
    def _coverage(view: ResolvedGraphView, artifacts: Mapping[str, RepositoryCommunicationArtifact]) -> dict[str, Any]:
        members = {}
        for member in view.members:
            if isinstance(member, SnapshotHandle):
                artifact = artifacts.get(member.snapshot_id)
                members[member.member_id] = artifact.completeness if artifact else "unavailable"
            else:
                members[member.member_id] = member.availability
        complete = all(value == "complete" for value in members.values()) if members else False
        return {"status": "complete" if complete else "partial", "members": members,
                "unknown_boundaries": [] if complete else [key for key, value in members.items() if value != "complete"]}

    @staticmethod
    def _resources(members: list[SnapshotHandle], artifacts: Mapping[str, RepositoryCommunicationArtifact]) -> list[dict[str, Any]]:
        result = []
        for member in members:
            artifact = artifacts[member.snapshot_id]
            for observation in artifact.resource_accesses:
                item = {"source_member_id": member.member_id, "observation_id": observation.observation_id,
                        "resource": dict(observation.payload), "evidence": TopologyArtifactBuilder._evidence(observation)}
                item["edge_id"] = stable_edge_id("resource", item)
                result.append(item)
        return sorted(result, key=lambda item: item["edge_id"])

    def _append_message_dependencies(self, member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates):
        # Message matching is deliberately conservative until broker-specific
        # collectors provide normalized delivery semantics.  Exact protocol +
        # channel matches are safe; empty channels never wildcard consumers.
        consumers = []
        for target in available:
            for observation in getattr(artifacts[target.snapshot_id], "message_subscriptions", ()):
                consumers.append((target, observation))
        for publication in artifact.message_publications:
            channel = dict(publication.payload).get("messaging", {}).get("channel", "")
            if not channel:
                candidates.append({"kind": "unresolved", "observation_id": publication.observation_id,
                                   "source_member_id": member.member_id, "reason": "empty_channel"})
                continue
            for target, subscription in consumers:
                if target.member_id == member.member_id:
                    continue
                target_channel = dict(subscription.payload).get("messaging", {}).get("channel", "")
                if channel != target_channel:
                    continue
                dependency = {"kind": "message", "source_member_id": member.member_id,
                              "target_member_id": target.member_id,
                              "observation_ids": [publication.observation_id, subscription.observation_id],
                              "channel": channel}
                dependency["edge_id"] = stable_edge_id("message", dependency)
                endpoint_dependencies.append(dependency)
                key = (member.member_id, target.member_id, "message")
                if key not in service_edges:
                    projection = {"kind": "message", "source_member_id": member.member_id,
                                  "target_member_id": target.member_id,
                                  "supporting_endpoint_dependency_ids": [dependency["edge_id"]]}
                    projection["edge_id"] = stable_edge_id("service", projection)
                    service_edges[key] = projection
