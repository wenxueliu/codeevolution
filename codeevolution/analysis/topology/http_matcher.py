"""Deterministic, identity-first matching for static HTTP observations.

This module deliberately has no database, filesystem, or live-repository
dependencies.  The topology worker supplies frozen observation and endpoint
facts from a :class:`ResolvedGraphView`; the matcher only decides whether the
facts justify a service/endpoint dependency.  Keeping this boundary pure is
important: a relative URL must never become an internal edge merely because a
similarly shaped route happens to exist in another repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

_CONFIRMED = "confirmed"
_CANDIDATE = "candidate"
_UNRESOLVED = "unresolved"
_AMBIGUOUS = "ambiguous"
_OUT_OF_SCOPE = "out_of_scope_or_unregistered"
_KNOWN_EXTERNAL = "known_external"


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
class KnownExternalRule:
    """One explicit rule identifying a non-Scope HTTP authority.

    A rule is intentionally narrower than a service alias.  It can identify a
    whole authority, or constrain the authority to a route/method.  Nothing is
    inferred from provider names, URL shape, or the fact that an authority is
    unregistered; the authority must match this frozen rule exactly.
    """

    rule_id: str
    authority: str
    provider: str = ""
    methods: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    source: str = "configured"
    protocols: tuple[str, ...] = ("http",)

    def __post_init__(self) -> None:
        rule_id = self.rule_id.strip()
        if not rule_id:
            raise ValueError("known external rule_id must not be blank")
        object.__setattr__(self, "rule_id", rule_id)
        authority = canonical_authority(self.authority)
        if not authority:
            raise ValueError("known external rule authority must be a valid host")
        object.__setattr__(self, "authority", authority)
        methods = tuple(sorted({str(item).strip().upper() for item in self.methods if str(item).strip()}))
        object.__setattr__(self, "methods", methods)
        prefixes = tuple(sorted({_external_path_prefix(item) for item in self.path_prefixes}))
        object.__setattr__(self, "path_prefixes", prefixes)
        protocols = tuple(sorted({str(item).strip().lower() for item in self.protocols if str(item).strip()}))
        if not protocols:
            raise ValueError("known external rule protocols must not be empty")
        object.__setattr__(self, "protocols", protocols)
        object.__setattr__(self, "provider", self.provider.strip())
        source = self.source.strip()
        if not source:
            raise ValueError("known external rule source must not be blank")
        object.__setattr__(self, "source", source)

    def matches(
        self,
        *,
        authority: str,
        method: str | None,
        path: str | None,
        protocol: str = "http",
    ) -> bool:
        if canonical_authority(authority) != self.authority:
            return False
        if protocol.lower() not in self.protocols:
            return False
        if self.methods and (not method or method.upper() not in self.methods):
            return False
        if self.path_prefixes:
            normalized = normalize_path(path or "")
            if not normalized or not any(_path_prefix_matches(normalized, prefix) for prefix in self.path_prefixes):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "authority": self.authority,
            "provider": self.provider,
            "methods": list(self.methods),
            "path_prefixes": list(self.path_prefixes),
            "source": self.source,
            "protocols": list(self.protocols),
        }


@dataclass(frozen=True)
class KnownExternalRegistry:
    """Deterministic, immutable known-external configuration for a View.

    The registry is deployment input, not a discovery result.  Its digest is
    included in topology identity by the builder/application service so a
    registry change cannot silently reuse an old artifact.
    """

    rules: tuple[KnownExternalRule, ...] = ()

    def __post_init__(self) -> None:
        rules = tuple(sorted(self.rules, key=lambda item: item.rule_id))
        if len({item.rule_id for item in rules}) != len(rules):
            raise ValueError("known external rule_id values must be unique")
        object.__setattr__(self, "rules", rules)

    @classmethod
    def from_value(cls, value: Any) -> "KnownExternalRegistry":
        """Coerce a JSON-shaped registry without accepting implicit variants."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            raw_rules = value.get("rules", ())
            if not isinstance(raw_rules, (list, tuple)):
                raise ValueError("known external registry rules must be a list")
        elif isinstance(value, (list, tuple)):
            raw_rules = value
        else:
            raise ValueError("known external registry must be an object or list")
        rules: list[KnownExternalRule] = []
        for index, raw in enumerate(raw_rules):
            if isinstance(raw, str):
                raw = {"rule_id": f"external-{index + 1}", "authority": raw}
            if not isinstance(raw, Mapping):
                raise ValueError("known external registry rule must be an object")
            methods = raw.get("methods", raw.get("method", ()))
            if isinstance(methods, str):
                methods = (methods,)
            prefixes = raw.get("path_prefixes", raw.get("path_prefix", ()))
            if isinstance(prefixes, str):
                prefixes = (prefixes,)
            protocols = raw.get("protocols", raw.get("protocol", ("http",)))
            if isinstance(protocols, str):
                protocols = (protocols,)
            rules.append(KnownExternalRule(
                rule_id=str(raw.get("rule_id", raw.get("id", f"external-{index + 1}"))),
                authority=str(raw.get("authority", raw.get("host", ""))),
                provider=str(raw.get("provider", raw.get("name", ""))),
                methods=tuple(methods or ()),
                path_prefixes=tuple(prefixes or ()),
                source=str(raw.get("source", "configured")),
                protocols=tuple(protocols or ("http",)),
            ))
        return cls(tuple(rules))

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {"schema": "known-external-registry/v1", "rules": [item.to_dict() for item in self.rules]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(payload).hexdigest()

    def matches(self, observation: "HttpObservation") -> tuple[KnownExternalRule, ...]:
        return self.matches_identity(
            authority=observation.authority or observation.client_binding_authority or "",
            protocol="http",
            method=observation.method,
            path=observation.path,
        )

    def matches_identity(
        self,
        *,
        authority: str,
        protocol: str,
        method: str | None = None,
        path: str | None = None,
    ) -> tuple[KnownExternalRule, ...]:
        if not authority:
            return ()
        return tuple(
            rule for rule in self.rules
            if rule.matches(authority=authority, protocol=protocol, method=method, path=path)
        )


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
    external_rule_id: str | None = None
    external_provider: str | None = None
    external_rule_source: str | None = None

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

    def __init__(
        self,
        rules: HttpMatchingRules | None = None,
        known_external_registry: KnownExternalRegistry | Mapping[str, Any] | Iterable[Any] | None = None,
    ):
        self.rules = rules or HttpMatchingRules()
        self.known_external_registry = KnownExternalRegistry.from_value(known_external_registry)

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
        external_rules = self.known_external_registry.matches(fact)
        if len(alias_members) > 1:
            return self._decision(
                _AMBIGUOUS,
                fact,
                candidates=tuple(sorted(alias_members)),
                reasons=("ambiguous_alias",),
            )
        if alias_members and external_rules:
            return self._decision(
                _AMBIGUOUS,
                fact,
                candidates=tuple(sorted(alias_members | {item.rule_id for item in external_rules})),
                reasons=("internal_alias_conflicts_with_known_external_rule",),
            )
        if len(external_rules) > 1:
            return self._decision(
                _AMBIGUOUS,
                fact,
                candidates=tuple(item.rule_id for item in external_rules),
                reasons=("ambiguous_known_external_rule",),
            )
        if not alias_members and external_rules:
            rule = external_rules[0]
            return self._decision(
                _KNOWN_EXTERNAL,
                fact,
                reasons=("known_external_registry_match",),
                external_rule_id=rule.rule_id,
                external_provider=rule.provider or None,
                external_rule_source=rule.source,
            )
        if not alias_members:
            same_authority_rules = tuple(
                rule for rule in self.known_external_registry.rules
                if canonical_authority(rule.authority) == authority
            )
            return self._decision(
                _OUT_OF_SCOPE,
                fact,
                reasons=(
                    "known_external_rule_constraints_not_matched"
                    if same_authority_rules else "authority_not_registered_in_view",
                ),
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
        external_rule_id: str | None = None,
        external_provider: str | None = None,
        external_rule_source: str | None = None,
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
            external_rule_id=external_rule_id,
            external_provider=external_provider,
            external_rule_source=external_rule_source,
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


def _external_path_prefix(value: str) -> str:
    normalized = normalize_path(value)
    if not normalized:
        raise ValueError("known external path_prefix must be an absolute path")
    return normalized


def _path_prefix_matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


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
    "KnownExternalRegistry",
    "KnownExternalRule",
    "canonical_authority",
    "join_paths",
    "normalize_path",
    "route_score",
]
