"""Deterministic, identity-first matching for static HTTP observations.

This module deliberately has no database, filesystem, or live-repository
dependencies.  The topology worker supplies frozen observation and endpoint
facts from a :class:`ResolvedGraphView`; the matcher only decides whether the
facts justify a service/endpoint dependency.  Keeping this boundary pure is
important: a relative URL must never become an internal edge merely because a
similarly shaped route happens to exist in another repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

_CONFIRMED = "confirmed"
_CANDIDATE = "candidate"
_UNRESOLVED = "unresolved"
_AMBIGUOUS = "ambiguous"
_OUT_OF_SCOPE = "out_of_scope_or_unregistered"


@dataclass(frozen=True)
class HttpInboundEndpoint:
    """A frozen inbound HTTP endpoint belonging to one graph-view member."""

    member_id: str
    entry_id: str
    method: str
    path: str
    base_path: str = ""

    @property
    def effective_path(self) -> str:
        return join_paths(self.base_path, self.path)


@dataclass(frozen=True)
class HttpAlias:
    """A declared or frozen-config authority alias for a service member."""

    member_id: str
    authority: str
    source: str = "declared"
    confidence: float = 0.90


@dataclass(frozen=True)
class HttpObservation:
    """The minimum frozen HTTP observation needed for cross-repo matching."""

    observation_id: str
    source_member_id: str
    source_entry_id: str
    method: str | None
    path: str | None
    authority: str | None = None
    client_binding_authority: str | None = None
    entry_reachable: bool = True
    extraction_confidence: float = 1.0


@dataclass(frozen=True)
class ConfidenceComponent:
    name: str
    score: float
    rule: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "score": self.score, "rule": self.rule}


@dataclass(frozen=True)
class HttpMatchDecision:
    """A typed result which can be projected into endpoint/service artifacts.

    ``status == confirmed`` is the only result allowed to enter the internal
    service graph.  Endpoint alternatives are allowed only when every tied
    endpoint belongs to the uniquely identified target member.
    """

    status: str
    observation_id: str
    target_member_id: str | None = None
    target_entry_ids: tuple[str, ...] = ()
    score: float = 0.0
    confidence_level: str = "low"
    confidence_components: tuple[ConfidenceComponent, ...] = ()
    reasons: tuple[str, ...] = ()
    endpoint_alternatives: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def is_confirmed(self) -> bool:
        return self.status == _CONFIRMED

    def confidence(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "level": self.confidence_level,
            "components": [item.as_dict() for item in self.confidence_components],
            "caps": [],
            "warnings": list(self.reasons),
        }


@dataclass(frozen=True)
class HttpMatchingRules:
    """Versioned thresholds for the pure HTTP matching algorithm."""

    version: str = "http-matcher/v1"
    confirmed_threshold: float = 0.80
    high_threshold: float = 0.90
    endpoint_margin: float = 0.10
    extraction_weight: float = 0.30
    reachability_weight: float = 0.20
    protocol_weight: float = 0.25
    target_identity_weight: float = 0.25


class HttpTopologyMatcher:
    """Match one HTTP observation against frozen aliases and inbound routes.

    Inputs may be the DTOs in this module or mappings shaped like the
    communication artifact.  Supporting mappings makes the matcher usable
    while snapshot and topology DTO migrations happen independently; the
    output remains typed and deterministic.
    """

    def __init__(self, rules: HttpMatchingRules | None = None):
        self.rules = rules or HttpMatchingRules()

    def match(
        self,
        observation: HttpObservation | Mapping[str, Any],
        endpoints: Iterable[HttpInboundEndpoint | Mapping[str, Any]],
        aliases: Iterable[HttpAlias | Mapping[str, Any]],
    ) -> HttpMatchDecision:
        fact = _coerce_observation(observation)
        frozen_endpoints = tuple(_coerce_endpoint(item) for item in endpoints)
        frozen_aliases = tuple(_coerce_alias(item) for item in aliases)

        if not fact.entry_reachable or not fact.source_entry_id:
            return self._decision(
                _UNRESOLVED,
                fact,
                reasons=("missing_entry_reachability",),
            )

        authority = canonical_authority(fact.authority or fact.client_binding_authority or "")
        if not authority:
            # A route-only URL does not identify a target service.  Route
            # similarity is intentionally not used to manufacture identity.
            return self._decision(
                _UNRESOLVED,
                fact,
                reasons=("relative_url_without_unique_client_binding",),
            )

        alias_members = _alias_members(authority, frozen_aliases)
        if len(alias_members) > 1:
            return self._decision(
                _AMBIGUOUS,
                fact,
                candidates=tuple(sorted(alias_members)),
                reasons=("ambiguous_alias",),
            )
        if not alias_members:
            return self._decision(
                _OUT_OF_SCOPE,
                fact,
                reasons=("authority_not_registered_in_view",),
            )

        target_member_id = next(iter(alias_members))
        if not fact.method:
            return self._decision(
                _CANDIDATE,
                fact,
                target_member_id=target_member_id,
                reasons=("unknown_http_method",),
            )
        request_path = normalize_path(fact.path or "")
        if not request_path:
            return self._decision(
                _CANDIDATE,
                fact,
                target_member_id=target_member_id,
                reasons=("unresolved_request_path",),
            )

        method = fact.method.upper()
        compatible = [
            endpoint
            for endpoint in frozen_endpoints
            if endpoint.member_id == target_member_id
            and endpoint.method.upper() == method
            and route_score(request_path, endpoint.effective_path) is not None
        ]
        if not compatible:
            return self._decision(
                _UNRESOLVED,
                fact,
                target_member_id=target_member_id,
                reasons=("no_compatible_target_endpoint",),
            )

        ranked = sorted(
            ((route_score(request_path, item.effective_path) or 0.0, item) for item in compatible),
            key=lambda pair: (-pair[0], pair[1].entry_id),
        )
        best_score = ranked[0][0]
        winners = tuple(item for score, item in ranked if score == best_score)
        runners_up = tuple(item for score, item in ranked if score < best_score)
        if runners_up and best_score - ranked[len(winners)][0] < self.rules.endpoint_margin:
            return self._decision(
                _CANDIDATE,
                fact,
                target_member_id=target_member_id,
                candidates=tuple(item.entry_id for _, item in ranked),
                reasons=("endpoint_score_margin_too_small",),
            )

        alias_confidence = max(
            alias.confidence
            for alias in frozen_aliases
            if alias.member_id == target_member_id
            and canonical_authority(alias.authority) == authority
        )
        components = (
            ConfidenceComponent("extraction", _clamp(fact.extraction_confidence), "frozen-observation"),
            ConfidenceComponent("reachability", 1.0, "entry-reachable-shortest-path"),
            ConfidenceComponent("protocol_identity", best_score, "http-method+route-template"),
            ConfidenceComponent("target_identity", _clamp(alias_confidence), "frozen-service-alias"),
        )
        score = self._weighted_score(components)
        if score < self.rules.confirmed_threshold:
            return self._decision(
                _CANDIDATE,
                fact,
                target_member_id=target_member_id,
                target_entry_ids=tuple(item.entry_id for item in winners),
                score=score,
                components=components,
                candidates=tuple(item.entry_id for _, item in ranked),
                reasons=("confidence_below_confirmed_threshold",),
            )
        return self._decision(
            _CONFIRMED,
            fact,
            target_member_id=target_member_id,
            target_entry_ids=tuple(item.entry_id for item in winners),
            score=score,
            components=components,
            endpoint_alternatives=tuple(item.entry_id for item in winners[1:]),
        )

    def _weighted_score(self, components: tuple[ConfidenceComponent, ...]) -> float:
        weights = (
            self.rules.extraction_weight,
            self.rules.reachability_weight,
            self.rules.protocol_weight,
            self.rules.target_identity_weight,
        )
        return round(sum(component.score * weight for component, weight in zip(components, weights)), 6)

    def _decision(
        self,
        status: str,
        observation: HttpObservation,
        *,
        target_member_id: str | None = None,
        target_entry_ids: tuple[str, ...] = (),
        score: float = 0.0,
        components: tuple[ConfidenceComponent, ...] = (),
        reasons: tuple[str, ...] = (),
        endpoint_alternatives: tuple[str, ...] = (),
        candidates: tuple[str, ...] = (),
    ) -> HttpMatchDecision:
        level = "high" if score >= self.rules.high_threshold else "medium" if score >= self.rules.confirmed_threshold else "low"
        return HttpMatchDecision(
            status=status,
            observation_id=observation.observation_id,
            target_member_id=target_member_id,
            target_entry_ids=target_entry_ids,
            score=score,
            confidence_level=level,
            confidence_components=components,
            reasons=reasons,
            endpoint_alternatives=endpoint_alternatives,
            candidates=candidates,
        )


def canonical_authority(value: str) -> str:
    """Normalize a declared authority without inventing name variants."""
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if not parsed.hostname:
        return ""
    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        return ""
    return f"{host}:{port}" if port is not None else host


def normalize_path(value: str) -> str:
    """Normalize a route path while retaining parameter and wildcard syntax."""
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return ""
    pieces = [piece for piece in path.split("/") if piece]
    return "/" + "/".join(pieces) if pieces else "/"


def join_paths(base_path: str, path: str) -> str:
    base = normalize_path(base_path) if base_path else ""
    route = normalize_path(path)
    if not base:
        return route
    if not route or route == "/":
        return base
    return normalize_path(f"{base}/{route.lstrip('/')}")


def route_score(actual: str, template: str) -> float | None:
    """Return an explainable route compatibility score or ``None``.

    Static segments are strongest, named parameters are next, and framework
    wildcards are weakest.  Matching never uses substring semantics.
    """
    actual_parts = _path_parts(actual)
    template_parts = _path_parts(template)
    if not actual_parts and not template_parts:
        return 1.0
    if not template_parts:
        return None
    static = parameters = wildcards = 0
    index = 0
    while index < len(template_parts):
        segment = template_parts[index]
        if _is_multi_wildcard(segment):
            wildcards += max(1, len(actual_parts) - index)
            total = static + parameters + wildcards
            return round((static + parameters * 0.90 + wildcards * 0.80) / total, 6)
        if index >= len(actual_parts):
            return None
        if _is_single_wildcard(segment):
            wildcards += 1
        elif _is_parameter(segment):
            parameters += 1
        elif segment.lower() == actual_parts[index].lower():
            static += 1
        else:
            return None
        index += 1
    if index != len(actual_parts):
        return None
    total = static + parameters + wildcards
    if not total:
        return None
    # The minimum compatible template score is still protocol compatible; the
    # threshold is applied to the combined, explainable confidence instead.
    return round((static + parameters * 0.90 + wildcards * 0.80) / total, 6)


def _path_parts(path: str) -> tuple[str, ...]:
    normalized = normalize_path(path)
    return tuple(part for part in normalized.split("/") if part)


def _is_parameter(segment: str) -> bool:
    return (segment.startswith(":") and len(segment) > 1) or (
        segment.startswith("{") and segment.endswith("}") and len(segment) > 2
    ) or (segment.startswith("<") and segment.endswith(">") and len(segment) > 2)


def _is_single_wildcard(segment: str) -> bool:
    return segment in {"*", "{*path}", "<path>", "<string:path>"}


def _is_multi_wildcard(segment: str) -> bool:
    return segment in {"**", "{**path}", "<path:path>", "<path:rest>"}


def _alias_members(authority: str, aliases: Iterable[HttpAlias]) -> set[str]:
    return {
        alias.member_id
        for alias in aliases
        if canonical_authority(alias.authority) == authority
    }


def _coerce_observation(value: HttpObservation | Mapping[str, Any]) -> HttpObservation:
    if isinstance(value, HttpObservation):
        return value
    request = value.get("request", value)
    binding = value.get("client_binding", value.get("clientBinding", {}))
    entry = value.get("entry_ref", value.get("entry", {}))
    return HttpObservation(
        observation_id=str(value.get("observation_id", value.get("id", ""))),
        source_member_id=str(value.get("source_member_id", entry.get("member_id", ""))),
        source_entry_id=str(value.get("source_entry_id", entry.get("entry_id", ""))),
        method=_string_or_none(request.get("method")),
        path=_string_or_none(request.get("normalized_path", request.get("path"))),
        authority=_string_or_none(request.get("authority")),
        client_binding_authority=_string_or_none(
            binding.get("base_authority", binding.get("authority"))
        ),
        entry_reachable=bool(value.get("entry_reachable", bool(entry))),
        extraction_confidence=float(value.get("extraction_confidence", 1.0)),
    )


def _coerce_endpoint(value: HttpInboundEndpoint | Mapping[str, Any]) -> HttpInboundEndpoint:
    if isinstance(value, HttpInboundEndpoint):
        return value
    return HttpInboundEndpoint(
        member_id=str(value["member_id"]),
        entry_id=str(value["entry_id"]),
        method=str(value["method"]),
        path=str(value["path"]),
        base_path=str(value.get("base_path", "")),
    )


def _coerce_alias(value: HttpAlias | Mapping[str, Any]) -> HttpAlias:
    if isinstance(value, HttpAlias):
        return value
    return HttpAlias(
        member_id=str(value["member_id"]),
        authority=str(value.get("authority", value.get("value", ""))),
        source=str(value.get("source", "declared")),
        confidence=float(value.get("confidence", 0.90)),
    )


def _string_or_none(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "ConfidenceComponent",
    "HttpAlias",
    "HttpInboundEndpoint",
    "HttpMatchDecision",
    "HttpMatchingRules",
    "HttpObservation",
    "HttpTopologyMatcher",
    "canonical_authority",
    "join_paths",
    "normalize_path",
    "route_score",
]
