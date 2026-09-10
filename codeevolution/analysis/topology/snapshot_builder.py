"""Build a deterministic cross-service topology from frozen snapshot facts.

The builder is intentionally free of repository paths, live CodeGraph reads and
HTTP delivery concerns.  Its only inputs are a resolved Graph View and the
immutable communication artifacts belonging to the available members in that
View.  This makes a topology reproducible after a checkout is deleted.
"""

from __future__ import annotations

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
    KnownExternalRegistry,
    canonical_authority,
)
from codeevolution.domain.topology import (
    ResolvedGraphView,
    SnapshotHandle,
    UnavailableMember,
    canonical_digest,
    stable_edge_id,
)


def _authority_matches_alias(authority: str, alias: str) -> bool:
    normalized_authority = canonical_authority(authority)
    normalized_alias = canonical_authority(alias)
    return bool(normalized_authority and normalized_authority == normalized_alias)


def _message_match_key(
    messaging: Mapping[str, Any], *, include_channel: bool = True, include_routing_key: bool = True
) -> tuple[Any, ...]:
    """Return the broker identity fields required for safe message pairing."""
    names = (
            "protocol",
            "broker_instance_hint",
            "destination_kind",
            "exchange",
            "queue",
            "consumer_group",
        )
    if include_routing_key:
        names = names[:4] + ("routing_key",) + names[4:]
    if include_channel:
        names = names + ("channel",)
    return tuple(messaging.get(name) for name in names)


def _message_channel_matches(protocol: str, source: str, target: str) -> bool:
    """Apply only broker wildcard semantics that are statically unambiguous."""
    if not source or not target:
        return False
    if source == target:
        return True
    protocol = str(protocol or "").lower()
    if protocol == "rabbitmq":
        source_parts, target_parts = source.split("."), target.split(".")
        if len(source_parts) != len(target_parts):
            return False
        return all(a == b or a == "*" or b == "*" for a, b in zip(source_parts, target_parts))
    if protocol == "nats":
        source_parts, target_parts = source.split("."), target.split(".")
        for left, right in zip(source_parts, target_parts):
            if left in {"*", ">"} or right in {"*", ">"}:
                if left == ">" or right == ">":
                    return True
                continue
            if left != right:
                return False
        return len(source_parts) == len(target_parts)
    return False


def _message_routing_matches(protocol: str, source: str | None, target: str | None) -> bool:
    if not source or not target or source == target:
        return bool(source == target or not source and not target)
    return protocol.lower() == "rabbitmq" and _message_channel_matches(protocol, source, target)


def _plain_json(value: Any) -> Any:
    """Materialize immutable artifact mappings before API/CAS serialization."""
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


class TopologyBuildError(RuntimeError):
    """Raised when a bound Snapshot cannot supply its immutable artifact."""


class TopologyArtifactBuilder:
    """Project repository communication facts into one Scope topology."""

    def __init__(
        self,
        matcher: HttpTopologyMatcher | None = None,
        known_external_registry: KnownExternalRegistry | Mapping[str, Any] | list[Any] | None = None,
    ):
        registry = KnownExternalRegistry.from_value(known_external_registry)
        if matcher is not None and known_external_registry is not None:
            matcher_registry = getattr(matcher, "known_external_registry", registry)
            if matcher_registry.digest != registry.digest:
                raise ValueError("matcher and builder known external registries differ")
        self.http_matcher = matcher or HttpTopologyMatcher(known_external_registry=registry)
        self.known_external_registry = getattr(self.http_matcher, "known_external_registry", registry)
        self.known_external_registry_digest = self.known_external_registry.digest

    def build(
        self,
        view: ResolvedGraphView,
        artifacts: Mapping[str, RepositoryCommunicationArtifact],
    ) -> dict[str, Any]:
        available = [member for member in view.members if isinstance(member, SnapshotHandle)]
        missing = [member for member in view.members if isinstance(member, UnavailableMember)]
        topology_rules_digest = getattr(view, "topology_rules_digest", "")
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
        message_alternatives: list[dict[str, Any]] = []
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
                    "rules_digest": topology_rules_digest,
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
                elif decision.status in {"out_of_scope_or_unregistered", "known_external"}:
                    boundary = {
                        **base,
                        "kind": decision.status,
                        "reason": decision.reasons[0] if decision.reasons else "",
                    }
                    if decision.external_rule_id:
                        boundary["external_rule_id"] = decision.external_rule_id
                    if decision.external_provider:
                        boundary["external_provider"] = decision.external_provider
                    if decision.external_rule_source:
                        boundary["external_rule_source"] = decision.external_rule_source
                    boundary["boundary_id"] = stable_edge_id("boundary", boundary)
                    boundary_dependencies.append(boundary)

            self._append_message_dependencies(
                member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates,
                message_alternatives, topology_rules_digest,
            )
            self._append_grpc_dependencies(
                member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates,
                boundary_dependencies, topology_rules_digest,
            )

        for projection in service_edges.values():
            projection["supporting_endpoint_dependency_ids"].sort()
        endpoint_dependencies.sort(key=lambda item: item["edge_id"])
        boundary_dependencies.sort(key=lambda item: item["boundary_id"])
        candidates.sort(key=lambda item: (item["kind"], item["observation_id"]))
        services = [
            self._service(member, artifacts.get(member.snapshot_id) if isinstance(member, SnapshotHandle) else None)
            for member in sorted(view.members, key=lambda item: item.member_id)
        ]
        coverage = self._coverage(view, artifacts)
        payload = {
            "schema_version": "topology-artifact/v1",
            "artifact_kind": "topology",
            "view_digest": view.view_digest,
            "scope_id": view.scope_id,
            "rules_digest": topology_rules_digest,
            "known_external_registry_digest": self.known_external_registry_digest,
            "members": services,
            "services": services,
            "endpoint_dependencies": endpoint_dependencies,
            "service_projections": sorted(service_edges.values(), key=lambda item: item["edge_id"]),
            "message_alternatives": sorted(message_alternatives, key=lambda item: item["alternative_group_id"]),
            "resource_dependencies": self._resources(available, artifacts, topology_rules_digest),
            "boundary_dependencies": boundary_dependencies,
            "candidates": candidates,
            "coverage": coverage,
            "warnings": ["partial_view_coverage"] if missing else [],
            "identity": {
                "view_digest": view.view_digest,
                "rules_digest": topology_rules_digest,
                "known_external_registry_digest": self.known_external_registry_digest,
            },
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
        return _plain_json(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _evidence(observation: CommunicationObservation) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if observation.callsite:
            result["callsite"] = observation.callsite.to_dict()
        if observation.caller:
            result["caller"] = observation.caller.to_dict()
        if observation.call_path:
            result["call_path"] = [item.to_dict() for item in observation.call_path]
        if observation.call_path_evidence is not None:
            result["call_path_evidence"] = observation.call_path_evidence.to_dict()
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
        member_details = {}
        for member in sorted(view.members, key=lambda item: item.member_id):
            if isinstance(member, SnapshotHandle):
                artifact = artifacts.get(member.snapshot_id)
                members[member.member_id] = artifact.completeness if artifact else "unavailable"
                member_details[member.member_id] = {
                    "status": artifact.completeness if artifact else "unavailable",
                    "collectors": [item.to_dict() for item in (artifact.collector_coverage if artifact else ())],
                }
            else:
                members[member.member_id] = member.availability
                member_details[member.member_id] = {
                    "status": member.availability,
                    "reason": member.reason,
                    "collectors": [],
                }
        complete = all(value == "complete" for value in members.values()) if members else False
        return {"status": "complete" if complete else "partial", "members": members,
                "member_details": member_details,
                "unknown_boundaries": [] if complete else [key for key, value in members.items() if value != "complete"]}

    @staticmethod
    def _resources(
        members: list[SnapshotHandle],
        artifacts: Mapping[str, RepositoryCommunicationArtifact],
        rules_digest: str = "",
    ) -> list[dict[str, Any]]:
        result = []
        for member in members:
            artifact = artifacts[member.snapshot_id]
            for observation in artifact.resource_accesses:
                item = {"source_member_id": member.member_id, "observation_id": observation.observation_id,
                        "resource": _plain_json(observation.payload.get("resource", observation.payload)),
                        "rules_digest": rules_digest,
                        "evidence": TopologyArtifactBuilder._evidence(observation)}
                item["edge_id"] = stable_edge_id("resource", item)
                result.append(item)
        return sorted(result, key=lambda item: item["edge_id"])

    def _append_message_dependencies(self, member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates, alternatives, rules_digest):
        # Message matching is deliberately conservative until broker-specific
        # collectors provide normalized delivery semantics.  Exact protocol +
        # channel matches are safe; empty channels never wildcard consumers.
        consumers = []
        for target in available:
            for observation in getattr(artifacts[target.snapshot_id], "message_subscriptions", ()):
                consumers.append((target, observation))
        for publication in artifact.message_publications:
            messaging = dict(publication.payload).get("messaging", {})
            channel = messaging.get("channel", "")
            protocol = messaging.get("protocol", "")
            delivery = messaging.get("delivery_semantics", "unknown")
            if not channel:
                candidates.append({"kind": "unresolved", "observation_id": publication.observation_id,
                                   "source_member_id": member.member_id, "reason": "empty_channel"})
                continue
            matched_competing: list[tuple[Any, Any]] = []
            matched_identity = False
            for target, subscription in consumers:
                if target.member_id == member.member_id:
                    continue
                target_messaging = dict(subscription.payload).get("messaging", {})
                if _message_match_key(messaging, include_channel=False, include_routing_key=False) != _message_match_key(
                    target_messaging, include_channel=False, include_routing_key=False
                ):
                    continue
                if not _message_channel_matches(protocol, channel, str(target_messaging.get("channel") or "")):
                    continue
                if not _message_routing_matches(
                    protocol,
                    messaging.get("routing_key"),
                    target_messaging.get("routing_key"),
                ):
                    continue
                matched_identity = True
                if delivery not in {"broadcast", "fanout"} or target_messaging.get("delivery_semantics", "unknown") not in {"broadcast", "fanout"}:
                    if delivery == "competing" and target_messaging.get("delivery_semantics") == "competing":
                        matched_competing.append((target, subscription))
                        continue
                    candidates.append({
                        "kind": "candidate",
                        "observation_id": publication.observation_id,
                        "source_member_id": member.member_id,
                        "target_member_id": target.member_id,
                        "channel": channel,
                        "reason": "unknown_delivery_semantics",
                    })
                    continue
                dependency = {
                    "kind": "message",
                    "source_member_id": member.member_id,
                    "target_member_id": target.member_id,
                    "source": {
                        "member_id": member.member_id,
                        "entry_id": publication.entry_id or channel,
                    },
                    "target": {
                        "member_id": target.member_id,
                        "entry_ids": [subscription.entry_id or channel],
                    },
                    "observation_ids": [publication.observation_id, subscription.observation_id],
                    "channel": channel,
                    "rules_digest": rules_digest,
                }
                dependency["edge_id"] = stable_edge_id("message", dependency)
                endpoint_dependencies.append(dependency)
                key = (member.member_id, target.member_id, "message")
                if key not in service_edges:
                    projection = {"kind": "message", "source_member_id": member.member_id,
                                  "target_member_id": target.member_id,
                                  "supporting_endpoint_dependency_ids": [dependency["edge_id"]]}
                    projection["edge_id"] = stable_edge_id("service", projection)
                    service_edges[key] = projection
            if matched_competing:
                group = {
                    "channel": channel,
                    "protocol": protocol,
                    "source_member_id": member.member_id,
                    "consumer_member_ids": sorted({target.member_id for target, _ in matched_competing}),
                    "consumer_entry_ids": sorted({subscription.entry_id for _, subscription in matched_competing if subscription.entry_id}),
                    "rules_digest": rules_digest,
                }
                group["alternative_group_id"] = stable_edge_id("message-alternative", group)
                alternatives.append(group)
            elif not matched_identity:
                candidates.append({
                    "kind": "unresolved",
                    "observation_id": publication.observation_id,
                    "source_member_id": member.member_id,
                    "channel": channel,
                    "protocol": protocol,
                    "reason": "no_matching_consumer",
                })

    def _append_grpc_dependencies(
        self, member, artifact, available, artifacts, endpoint_dependencies, service_edges, candidates,
        boundary_dependencies, rules_digest
    ):
        """Match gRPC only when a frozen authority alias identifies one target.

        Generated-stub/proto descriptors are represented in the observation
        payload when available.  A bare callee name is deliberately retained as
        a candidate instead of creating a service edge.
        """
        for observation in artifact.grpc_clients:
            rpc = dict(observation.payload).get("rpc", {})
            method = str(rpc.get("fully_qualified_method") or rpc.get("method") or "")
            authority = str(rpc.get("authority") or "")
            if not method or not authority:
                candidates.append({
                    "kind": "candidate",
                    "observation_id": observation.observation_id,
                    "source_member_id": member.member_id,
                    "reason": "grpc_identity_unresolved",
                })
                continue
            aliases = [
                target for target in available
                if any(_authority_matches_alias(authority, alias) for alias in target.declared_aliases)
            ]
            external_rules = self.known_external_registry.matches_identity(
                authority=authority, protocol="grpc", method=method
            )
            if aliases and external_rules:
                candidates.append({
                    "kind": "ambiguous",
                    "observation_id": observation.observation_id,
                    "source_member_id": member.member_id,
                    "authority": authority,
                    "candidate_member_ids": [item.member_id for item in aliases],
                    "candidate_external_rule_ids": [item.rule_id for item in external_rules],
                    "reason": "internal_alias_conflicts_with_known_external_rule",
                })
                continue
            if not aliases and len(external_rules) > 1:
                candidates.append({
                    "kind": "ambiguous",
                    "observation_id": observation.observation_id,
                    "source_member_id": member.member_id,
                    "authority": authority,
                    "candidate_external_rule_ids": [item.rule_id for item in external_rules],
                    "reason": "ambiguous_known_external_rule",
                })
                continue
            if len(aliases) != 1:
                if len(external_rules) == 1:
                    rule = external_rules[0]
                    boundary = {
                        "kind": "known_external",
                        "observation_id": observation.observation_id,
                        "source": {"member_id": member.member_id, "entry_id": observation.entry_id or method},
                        "authority": authority,
                        "rpc_method": method,
                        "external_rule_id": rule.rule_id,
                        "external_provider": rule.provider,
                        "external_rule_source": rule.source,
                        "reason": "known_external_registry_match",
                        "rules_digest": rules_digest,
                    }
                    boundary["boundary_id"] = stable_edge_id("boundary", boundary)
                    boundary_dependencies.append(boundary)
                    continue
                try:
                    canonical_rpc_authority = canonical_authority(authority)
                except ValueError:
                    canonical_rpc_authority = ""
                reason = "known_external_rule_constraints_not_matched" if any(
                    item.authority == canonical_rpc_authority for item in self.known_external_registry.rules
                ) else "authority_not_registered_in_view"
                if len(aliases) > 1:
                    candidates.append({
                        "kind": "ambiguous",
                        "observation_id": observation.observation_id,
                        "source_member_id": member.member_id,
                        "authority": authority,
                        "candidate_member_ids": [item.member_id for item in aliases],
                        "reason": reason,
                    })
                else:
                    boundary = {
                        "kind": "out_of_scope_or_unregistered",
                        "observation_id": observation.observation_id,
                        "source": {"member_id": member.member_id, "entry_id": observation.entry_id or method},
                        "authority": authority,
                        "rpc_method": method,
                        "reason": reason,
                        "rules_digest": rules_digest,
                    }
                    boundary["boundary_id"] = stable_edge_id("boundary", boundary)
                    boundary_dependencies.append(boundary)
                continue
            target = aliases[0]
            method_name = method.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
            target_entries = [
                entry for entry in artifacts[target.snapshot_id].entries
                if entry.protocol.lower() in {"grpc", "grpc_server", "rpc"}
            ]
            target_entries = [
                entry for entry in target_entries
                if entry.method == method_name or entry.path_template == method
                or entry.handler.name == method_name or entry.handler.qualified_name.endswith(method_name)
            ]
            if len(target_entries) != 1:
                candidates.append({
                    "kind": "candidate",
                    "observation_id": observation.observation_id,
                    "source_member_id": member.member_id,
                    "target_member_id": target.member_id,
                    "rpc_method": method,
                    "reason": "grpc_server_method_unresolved",
                })
                continue
            target_entry = target_entries[0]
            dependency = {
                "kind": "grpc",
                "source_member_id": member.member_id,
                "target_member_id": target.member_id,
                "source": {"member_id": member.member_id, "entry_id": observation.entry_id or method},
                "target": {"member_id": target.member_id, "entry_ids": [target_entry.entry_id]},
                "rpc_method": method,
                "observation_id": observation.observation_id,
                "rules_digest": rules_digest,
                "confidence": {"level": "high", "reasons": ["authority_alias", "fully_qualified_method"]},
            }
            dependency["edge_id"] = stable_edge_id("grpc", dependency)
            endpoint_dependencies.append(dependency)
            key = (member.member_id, target.member_id, "grpc")
            projection = service_edges.get(key)
            if projection is None:
                projection = {
                    "kind": "grpc",
                    "source_member_id": member.member_id,
                    "target_member_id": target.member_id,
                    "supporting_endpoint_dependency_ids": [],
                }
                projection["edge_id"] = stable_edge_id("service", projection)
                service_edges[key] = projection
            projection["supporting_endpoint_dependency_ids"].append(dependency["edge_id"])
