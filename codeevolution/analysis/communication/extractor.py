"""Snapshot-bound static communication fact extraction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

from codeevolution.analysis.communication.entry_collector import (
    collect_entries,
    reachable_call_paths,
)
from codeevolution.analysis.communication.schema import (
    CallPathEvidence,
    CollectorCoverage,
    CollectorResult,
    CollectorRuleSet,
    CollectorStatus,
    CommunicationObservation,
    EntryFact,
    Location,
    NodeRef,
    RepositoryCommunicationArtifact,
    build_communication_artifact,
)

HTTP_PATTERNS = (
    "requests.", "httpx.", "aiohttp.", "urllib", "fetch", "axios.", "RestTemplate.",
    "WebClient.", "Feign", "http.Get", "http.Post", "http.NewRequest",
)
MQ_PATTERNS = (
    "publish", "produce", "send", "basic_publish", "xadd", "xread", "subscribe", "consume",
)
RPC_PATTERNS = ("grpc", "Stub", "BlockingStub", "FutureStub", "ServiceClient", "RpcClient")
HARD_BUDGETS = {
    "entries": 5_000,
    "call_depth": 12,
    "call_nodes": 10_000,
    "call_edges": 50_000,
    "observations": 10_000,
    "payload_bytes": 64 * 1024,
}


class CommunicationFactExtractor:
    """Extract target-independent observations from frozen graph/source ports."""

    def collect(
        self,
        graph,
        sources,
        rules: CollectorRuleSet,
        *,
        snapshot_id: str,
        member_id: str | None = None,
    ) -> RepositoryCommunicationArtifact:
        self._payload_limit = min(
            HARD_BUDGETS["payload_bytes"],
            max(1024, _budget(rules, "payload_bytes", HARD_BUDGETS["payload_bytes"], minimum=1024)),
        )
        allowed_paths = _manifest_paths(sources)
        entries, entry_coverage = collect_entries(
            graph,
            snapshot_id,
            max_entries=_budget(rules, "entries", HARD_BUDGETS["entries"]),
            allowed_paths=allowed_paths,
            return_coverage=True,
        )
        paths, reachability_coverage = reachable_call_paths(
            graph,
            entries,
            max_depth=_budget(rules, "call_depth", HARD_BUDGETS["call_depth"], minimum=0),
            max_nodes=_budget(rules, "call_nodes", HARD_BUDGETS["call_nodes"]),
            max_edges=_budget(rules, "call_edges", HARD_BUDGETS["call_edges"]),
            callee_cache={},
            return_coverage=True,
        )
        functions = {item.node_id: item for item in graph.functions()}
        # Keep the manifest boundary available to every observation builder
        # without widening the public graph/source adapter contract.
        functions["__allowed_paths__"] = allowed_paths
        result = [
            self._http_result(graph, sources, entries, paths, reachability_coverage, functions, rules, entry_coverage),
            self._message_result(graph, sources, entries, paths, reachability_coverage, functions, rules, snapshot_id, entry_coverage),
            self._grpc_result(graph, sources, entries, paths, reachability_coverage, functions, rules, entry_coverage),
            self._resource_result(graph, sources, entries, paths, reachability_coverage, functions, rules, entry_coverage, member_id),
        ]
        return build_communication_artifact(
            snapshot_id=snapshot_id, rules_digest=rules.digest, results=result
        )

    def _http_result(self, graph, sources, entries, paths, reachability, functions, rules, entry_coverage):
        observations = []
        unresolved = []
        entry_by_id = {entry.entry_id: entry for entry in entries}
        if not _protocol_supported(graph, rules, "http_client") or not callable(getattr(graph, "http_client_calls", None)):
            return CollectorResult(
                CollectorCoverage("http", "http-collector/v1", CollectorStatus.UNSUPPORTED, reason="graph_adapter_missing_http_client_calls"),
                entries=entries,
            )
        for pattern in HTTP_PATTERNS:
            for row in graph.http_client_calls(pattern):
                caller_id = row.get("caller_node_id")
                caller = functions.get(caller_id)
                call_line = int(row.get("call_line") or row.get("caller_line") or 1)
                payload = _http_payload(graph, sources, caller, call_line, row.get("callee_name", ""))
                for entry in _entries_for_node(caller_id, paths, entry_by_id):
                    observation = self._observation(
                        "http", entry, caller, call_line, payload, row,
                        extraction_confidence=0.95 if payload.get("request", {}).get("raw_url") else 0.45,
                        call_path=paths.get(entry.entry_id, {}).get(caller_id, []),
                        call_path_meta=_path_meta(reachability, entry.entry_id, caller_id),
                        functions=functions,
                    )
                    (observations if payload.get("request", {}).get("raw_url") else unresolved).append(observation)
        coverage = _collector_coverage("http", "http-collector/v1", entries, entry_coverage, reachability)
        return _bounded_result(
            CollectorResult(coverage, entries=entries, http_outbounds=tuple(_unique(observations)), unresolved_observations=tuple(_unique(unresolved))),
            rules,
        )

    def _message_result(self, graph, sources, entries, paths, reachability, functions, rules, snapshot_id, entry_coverage):
        publications, subscriptions, unresolved = [], [], []
        synthetic_entries = []
        entry_by_node = {entry.handler.node_id: entry for entry in entries}
        entry_by_id = {entry.entry_id: entry for entry in entries}
        if not _protocol_supported(graph, rules, "message") or not callable(getattr(graph, "mq_producer_calls", None)) or not callable(getattr(graph, "mq_consumers", None)):
            return CollectorResult(
                CollectorCoverage("message", "message-collector/v1", CollectorStatus.UNSUPPORTED, reason="graph_adapter_missing_message_calls"),
            )
        for pattern in MQ_PATTERNS:
            for row in graph.mq_producer_calls(pattern):
                caller = functions.get(row.get("caller_node_id"))
                call_line = int(row.get("call_line") or row.get("start_line") or 1)
                channel = _channel(sources, caller, call_line)
                for entry in _entries_for_node(row.get("caller_node_id"), paths, entry_by_id):
                    protocol = _broker(row.get("callee_name", ""))
                    observation = self._observation(
                        "message", entry, caller, call_line,
                        _message_payload(
                            row,
                            protocol=protocol,
                            channel=channel,
                            direction="publish",
                        ),
                        row, extraction_confidence=0.9 if channel else 0.35,
                        call_path=paths.get(entry.entry_id, {}).get(row.get("caller_node_id"), []),
                        call_path_meta=_path_meta(reachability, entry.entry_id, row.get("caller_node_id")),
                        functions=functions,
                    )
                    (publications if channel else unresolved).append(observation)
            for row in graph.mq_consumers(pattern):
                if not row.get("node_id"):
                    continue
                matched_entries = _entries_for_node(row["node_id"], paths, entry_by_id)
                if not matched_entries:
                    # A consumer is itself a production entry even when the
                    # graph adapter did not classify the framework decorator.
                    consumer = functions.get(row["node_id"])
                    if consumer is None:
                        continue
                    entry = entry_by_node.get(consumer.node_id)
                    if entry is None:
                        entry_id = f"entry:message:{consumer.node_id}"
                        entry = EntryFact(
                            entry_id=entry_id,
                            kind="message_consumer",
                            protocol="message",
                            handler=NodeRef(
                                snapshot_id=snapshot_id,
                                node_id=consumer.node_id,
                                kind=consumer.kind,
                                name=consumer.name,
                                qualified_name=consumer.qualified_name,
                                location=Location(consumer.file_path, max(1, consumer.start_line)),
                            ),
                            evidence={"entry_type": "message_consumer", "synthetic": True},
                        )
                        synthetic_entries.append(entry)
                        entry_by_node[consumer.node_id] = entry
                        paths[entry_id] = {consumer.node_id: [consumer.node_id]}
                        reachability[entry_id] = {
                            "truncated": False, "max_depth": 0, "max_nodes": 1,
                            "max_edges": 0, "edges_examined": 0,
                            "node_count": 1, "depth": {consumer.node_id: 0},
                            "edge_kinds": {consumer.node_id: []},
                            "alternative_shortest_path_count": {consumer.node_id: 0},
                            "reachability_rule": "shortest-call-path/v1",
                        }
                    matched_entries = (entry,)
                caller = functions.get(row["node_id"])
                channel = _channel(sources, caller, int(row.get("start_line") or 1))
                for entry in matched_entries:
                    protocol = _broker(row.get("name", ""))
                    observation = self._observation(
                        "message", entry, caller, int(row.get("start_line") or 1),
                        _message_payload(
                            row,
                            protocol=protocol,
                            channel=channel,
                            direction="consume",
                        ),
                        row, extraction_confidence=0.7 if channel else 0.3,
                        call_path=paths.get(entry.entry_id, {}).get(row.get("node_id"), []),
                        call_path_meta=_path_meta(reachability, entry.entry_id, row.get("node_id")),
                        functions=functions,
                    )
                    (subscriptions if channel else unresolved).append(observation)
        return _bounded_result(CollectorResult(
            _collector_coverage("message", "message-collector/v1", tuple(entries) + tuple(synthetic_entries), entry_coverage, reachability),
            entries=tuple(synthetic_entries),
            message_publications=tuple(_unique(publications)), message_subscriptions=tuple(_unique(subscriptions)),
            unresolved_observations=tuple(_unique(unresolved)),
        ), rules)

    def _grpc_result(self, graph, sources, entries, paths, reachability, functions, rules, entry_coverage):
        clients, unresolved = [], []
        if not _protocol_supported(graph, rules, "grpc") or not callable(getattr(graph, "rpc_calls", None)):
            return CollectorResult(CollectorCoverage("grpc", "grpc-collector/v1", CollectorStatus.UNSUPPORTED, reason="graph_adapter_missing_rpc_calls"))
        for pattern in RPC_PATTERNS:
            for row in graph.rpc_calls(pattern):
                caller = functions.get(row.get("caller_node_id"))
                call_line = int(row.get("call_line") or row.get("start_line") or 1)
                rpc = str(row.get("callee_name") or "")
                for entry in _entries_for_node(row.get("caller_node_id"), paths, {item.entry_id: item for item in entries}):
                    payload = _rpc_payload(sources, caller, call_line, rpc, row)
                    clients.append(self._observation(
                        "grpc", entry, caller, call_line,
                        payload, row,
                        extraction_confidence=0.65,
                        call_path=paths.get(entry.entry_id, {}).get(row.get("caller_node_id"), []),
                        call_path_meta=_path_meta(reachability, entry.entry_id, row.get("caller_node_id")), functions=functions,
                    ))
        return _bounded_result(CollectorResult(
            _collector_coverage("grpc", "grpc-collector/v1", entries, entry_coverage, reachability),
            grpc_clients=tuple(_unique(clients)), unresolved_observations=tuple(_unique(unresolved)),
        ), rules)

    def _resource_result(self, graph, sources, entries, paths, reachability, functions, rules, entry_coverage, member_id):
        accesses = []
        if not _protocol_supported(graph, rules, "resource") or not callable(getattr(graph, "database_call_candidates", None)):
            return CollectorResult(CollectorCoverage("resource", "resource-collector/v1", CollectorStatus.UNSUPPORTED, reason="graph_adapter_missing_resource_calls"))
        for row in graph.database_call_candidates():
            caller_id = row.get("caller_node_id")
            caller = functions.get(caller_id)
            operation = str(row.get("name") or row.get("target") or "unknown")
            if _is_message_operation(operation):
                # Redis Pub/Sub and Streams are communication observations;
                # do not duplicate them as shared data resources.
                continue
            if not _looks_like_resource(row, operation):
                continue
            for entry in _entries_for_node(caller_id, paths, {item.entry_id: item for item in entries}):
                resource_type = _resource_type(operation)
                accesses.append(self._observation(
                    "resource", entry, caller, int(row.get("call_line") or row.get("start_line") or 1),
                    {
                        "resource": {
                            "type": resource_type,
                            "operation": operation,
                            "instance_id": f"unresolved:{member_id or entry.handler.snapshot_id}:{resource_type}",
                            "access_mode": _resource_access_mode(operation),
                            "instance_resolution": "unresolved",
                        }
                    }, row,
                    extraction_confidence=0.55,
                    call_path=paths.get(entry.entry_id, {}).get(caller_id, []),
                    call_path_meta=_path_meta(reachability, entry.entry_id, caller_id), functions=functions,
                ))
        return _bounded_result(CollectorResult(
            _collector_coverage("resource", "resource-collector/v1", entries, entry_coverage, reachability),
            resource_accesses=tuple(_unique(accesses)),
        ), rules)

    def _observation(self, kind, entry, caller, call_line, payload, row, extraction_confidence, *, call_path=(), call_path_meta=None, functions=None):
        payload = _bounded_payload(payload, max_bytes=getattr(self, "_payload_limit", 65536))
        caller_ref = None
        callsite = None
        if caller is not None:
            caller_ref = NodeRef(
                snapshot_id=entry.handler.snapshot_id,
                node_id=caller.node_id,
                kind=caller.kind,
                name=caller.name,
                qualified_name=caller.qualified_name,
                location=(
                    Location(caller.file_path, max(1, caller.start_line))
                    if (functions or {}).get("__allowed_paths__") is None
                    or caller.file_path in (functions or {}).get("__allowed_paths__")
                    else None
                ),
            )
            if (functions or {}).get("__allowed_paths__") is None or caller.file_path in (functions or {}).get("__allowed_paths__"):
                callsite = Location(caller.file_path, max(1, call_line))
        identity = {
            "kind": kind,
            "entry_id": entry.entry_id,
            "caller": {
                "qualified_name": caller.qualified_name if caller else None,
                "file": caller.file_path if caller else row.get("file_path"),
            },
            "callsite": {"line": call_line},
            "callee": {
                "name": row.get("callee_name") or row.get("name") or row.get("target"),
                "file": row.get("callee_file") or row.get("file_path"),
                "line": row.get("callee_line"),
            },
            "payload": payload,
        }
        observation_id = f"observation:{kind}:sha256:{hashlib.sha256(_canonical_bytes(identity)).hexdigest()}"
        path_refs = []
        allowed_paths = (functions or {}).get("__allowed_paths__")
        for node_id in call_path:
            node = (functions or {}).get(node_id)
            if node is None:
                continue
            path_refs.append(
                NodeRef(
                    snapshot_id=entry.handler.snapshot_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    name=node.name,
                    qualified_name=node.qualified_name,
                    location=(
                        Location(node.file_path, max(1, node.start_line))
                        if allowed_paths is None or node.file_path in allowed_paths
                        else None
                    ),
                )
            )
        meta = call_path_meta or {}
        effective_depth = max(0, len(path_refs) - 1)
        effective_edges = tuple(meta.get("edge_kinds", []))[:effective_depth]
        path_evidence = CallPathEvidence(
            nodes=tuple(path_refs),
            edge_kinds=effective_edges,
            depth=effective_depth,
            truncated=bool(meta.get("truncated", False)),
            max_depth=max(effective_depth, int(meta.get("max_depth", effective_depth))),
            reachability_rule=str(meta.get("reachability_rule", "shortest-call-path/v1")),
            alternative_shortest_path_count=int(meta.get("alternative_shortest_path_count", 0)),
            entry_id=entry.entry_id,
            callsite=callsite,
        )
        return CommunicationObservation(
            observation_id, entry.entry_id, caller_ref, callsite, payload,
            extraction_confidence, tuple(path_refs), path_evidence,
        )


def _entries_for_node(node_id, paths, entries):
    return tuple(entries[entry_id] for entry_id in sorted(paths) if node_id in paths[entry_id] and entry_id in entries)


def _path_meta(reachability, entry_id, node_id):
    meta = reachability.get(entry_id, {})
    return {
        "truncated": meta.get("truncated", False),
        "max_depth": meta.get("max_depth", 0),
        "depth": meta.get("depth", {}).get(node_id, 0),
        "edge_kinds": meta.get("edge_kinds", {}).get(node_id, []),
        "alternative_shortest_path_count": meta.get("alternative_shortest_path_count", {}).get(node_id, 0),
        "reachability_rule": meta.get("reachability_rule", "shortest-call-path/v1"),
    }


def _manifest_paths(sources):
    list_files = getattr(sources, "list_files", None)
    if not callable(list_files):
        return None
    try:
        return frozenset(item.path for item in list_files())
    except Exception:
        return frozenset()


def _protocol_supported(graph, rules, protocol):
    support = rules.options.get("support", {}) if isinstance(rules.options, dict) else rules.options.get("support", {})
    primary_language = getattr(graph, "primary_language", None)
    if not callable(primary_language) or not support:
        return True
    language = str(primary_language() or "").lower().replace("javascript", "typescript")
    entry = support.get(language)
    if entry is None:
        return False
    return bool(entry.get(protocol, ()))


def _budget(rules, name: str, default: int, *, minimum: int = 1) -> int:
    """Read a collector budget while enforcing the process hard ceiling."""
    try:
        value = int(rules.budgets.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, HARD_BUDGETS.get(name, default)))


def _collector_coverage(name, rule, entries, entry_coverage, reachability):
    entry_truncated = bool(entry_coverage.get("truncated", False))
    reach_truncated = any(item.get("truncated", False) for item in reachability.values())
    status = CollectorStatus.TRUNCATED if entry_truncated or reach_truncated else CollectorStatus.COMPLETE
    return CollectorCoverage(
        name,
        rule,
        status,
        reason="bounded_reachability" if status is CollectorStatus.TRUNCATED else None,
        coverage={
            "entries": len(entries),
            "entry_collection": entry_coverage,
            "reachability": reachability,
        },
        truncated_count=(1 if entry_truncated else 0) + sum(1 for item in reachability.values() if item.get("truncated")),
    )


def _bounded_result(result, rules):
    """Apply one deterministic observation budget per collector."""
    budget = _budget(rules, "observations", HARD_BUDGETS["observations"])
    fields = (
        "http_outbounds", "message_publications", "message_subscriptions",
        "grpc_clients", "grpc_servers", "resource_accesses", "unresolved_observations",
    )
    changed = False
    updates = {}
    for field_name in fields:
        values = getattr(result, field_name)
        if len(values) > budget:
            updates[field_name] = tuple(sorted(values, key=lambda item: item.observation_id)[:budget])
            changed = True
    if not changed:
        return result
    coverage = replace(
        result.coverage,
        status=CollectorStatus.TRUNCATED,
        reason="observation_budget",
        truncated_count=result.coverage.truncated_count + 1,
        coverage={**dict(result.coverage.coverage), "observation_budget": budget},
    )
    return replace(result, coverage=coverage, **updates)


def _canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bounded_payload(payload, *, max_bytes=64 * 1024):
    raw = _canonical_bytes(payload)
    if len(raw) <= max_bytes:
        return payload
    return {
        "truncated": True,
        "payload_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "payload_bytes": len(raw),
    }


def _http_payload(graph, sources, caller, call_line, callee_name):
    text = sources.snippet(caller.file_path, max(1, call_line - 2), call_line + 2) if caller else None
    raw = _first_url(text or "")
    method = _method(callee_name)
    sanitized = _sanitize_url(raw)
    if sanitized.startswith("http"):
        parsed = urlsplit(sanitized)
        authority = parsed.netloc
        path = parsed.path or "/"
    else:
        authority, path = None, sanitized if sanitized.startswith("/") else None
    binding = None
    binding_candidate = None
    if path and not authority:
        base = _first_absolute_url(text or "")
        if not base and caller is not None and callable(getattr(graph, "url_candidate_nodes", None)):
            try:
                candidates = graph.url_candidate_nodes(caller.file_path, max(1, call_line - 10), call_line + 10)
            except Exception:
                candidates = []
            candidate_urls = {
                _sanitize_url(_candidate_text(item)): item
                for item in candidates
                if isinstance(item, dict) and _sanitize_url(_candidate_text(item)).startswith("http")
            }
            authorities = sorted({urlsplit(url).netloc for url in candidate_urls})
            if len(authorities) == 1:
                binding_candidate = next(iter(candidate_urls.values()), None)
                binding = {
                    "base_authority": authorities[0],
                    "source": "frozen_graph_config",
                    "resolution": "static",
                }
        elif base:
            binding = {"base_authority": urlsplit(_sanitize_url(base)).netloc, "source": "frozen_source", "resolution": "static"}
        if binding:
            authority = binding["base_authority"]
            config_key = _candidate_config_key(binding_candidate)
            if config_key:
                binding["config_key"] = config_key
    parsed = urlsplit(sanitized) if sanitized else None
    query_keys = sorted({key for key, _value in parse_qsl(parsed.query, keep_blank_values=True) if key}) if parsed else []
    scheme = (parsed.scheme or "") if parsed else ""
    if not scheme and binding and binding.get("base_authority"):
        base_scheme = _first_absolute_url(text or "")
        scheme = urlsplit(base_scheme).scheme if base_scheme else ""
    client = _client_metadata(callee_name)
    request = {
        "method": method,
        "method_resolution": "client_operation" if method else "unresolved",
        "raw_url": sanitized or None,
        "raw_url_template": sanitized or None,
        "scheme": scheme or None,
        "authority": authority,
        "normalized_path": path,
        "query_keys": query_keys,
        "resolution": "complete" if authority and path else "partial",
    }
    result = {"request": request}
    if client:
        result["client"] = client
    if binding:
        result["client_binding"] = binding
    return result


def _first_absolute_url(text):
    match = re.search(r"https?://[^\s'\"`),]+", text)
    return match.group(0).rstrip(")]}>.,") if match else ""


def _candidate_text(item):
    return str(item.get("value") or item.get("url") or item.get("signature") or item.get("qualified_name") or item.get("name") or "")


def _candidate_config_key(item):
    if not isinstance(item, dict):
        return None
    value = item.get("config_key") or item.get("key") or item.get("environment")
    return str(value).strip() if value else None


def _client_metadata(callee_name):
    value = str(callee_name or "")
    lower = value.lower()
    if not value:
        return None
    library = "unknown"
    for marker, name in (
        ("httpx", "httpx"), ("requests", "requests"), ("aiohttp", "aiohttp"),
        ("axios", "axios"), ("resttemplate", "spring-resttemplate"),
        ("webclient", "spring-webclient"), ("feign", "feign"),
        ("urllib", "urllib"), ("fetch", "fetch"),
    ):
        if marker in lower:
            library = name
            break
    operation = _method(value)
    return {"library": library, "operation": operation or value.rsplit(".", 1)[-1], "symbol": value}


def _sanitize_url(raw: str) -> str:
    """Drop URL credentials, query values and fragments before persistence."""
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme and not parsed.netloc:
        return parsed.path + _safe_query(parsed.query)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}{parsed.path or '/'}{_safe_query(parsed.query)}"


def _safe_query(query: str) -> str:
    if not query:
        return ""
    keys = [key for key, _value in parse_qsl(query, keep_blank_values=True) if key]
    return "?" + "&".join(sorted(set(keys))) if keys else ""


def _channel(sources, caller, line):
    if caller is None:
        return ""
    text = sources.snippet(caller.file_path, max(1, line - 2), line + 2) or ""
    match = re.search(r"['\"`]([A-Za-z0-9_.:/-]{2,})['\"`]", text)
    return match.group(1) if match else ""


def _message_payload(row, *, protocol, channel, direction):
    name = str(row.get("callee_name") or row.get("name") or row.get("target") or "")
    destination_kind = str(
        row.get("destination_kind")
        or ("stream" if protocol == "redis" and any(token in name.lower() for token in ("xadd", "xread")) else "topic" if protocol in {"kafka", "nats"} else "queue" if protocol == "rabbitmq" else "channel")
    )
    messaging = {
        "protocol": protocol,
        "broker_instance_hint": row.get("broker_instance_hint") or row.get("broker") or None,
        "destination_kind": destination_kind,
        "exchange": row.get("exchange"),
        "routing_key": row.get("routing_key"),
        "queue": row.get("queue"),
        "consumer_group": row.get("consumer_group") or row.get("group"),
        "event_type": row.get("event_type") or row.get("message_type"),
        "channel": channel,
        "delivery_semantics": _delivery_semantics(protocol, name, direction),
        "name_resolution": "literal" if channel else "unresolved",
    }
    return {"direction": direction, "messaging": messaging}


def _first_url(text):
    match = re.search(r"(?:https?://[^\s'\"`),]+|/api(?:/[^\s'\"`),]*)?)", text)
    return match.group(0).rstrip(")]}>,") if match else ""


def _method(name):
    lower = name.lower()
    for method in ("get", "post", "put", "patch", "delete", "head"):
        if lower.endswith("." + method) or lower.endswith("_" + method) or lower == method:
            return method.upper()
    return None


def _broker(name):
    lower = name.lower()
    if "kafka" in lower:
        return "kafka"
    if "rabbit" in lower or "amqp" in lower:
        return "rabbitmq"
    if "redis" in lower or "xadd" in lower or "xread" in lower:
        return "redis"
    if "nats" in lower:
        return "nats"
    return "unknown"


def _delivery_semantics(protocol, name, direction):
    lower = str(name).lower()
    if protocol == "redis":
        if any(token in lower for token in ("xreadgroup", "consumer_group", "group")):
            return "competing"
        if any(token in lower for token in ("publish", "subscribe")):
            return "broadcast"
    if direction == "consume" and any(token in lower for token in ("group", "worker", "queue")):
        return "competing"
    return "broadcast" if direction == "publish" and protocol == "kafka" else "unknown"


def _rpc_payload(sources, caller, line, method, row):
    qualified = str(row.get("callee_qualified_name") or method)
    text = sources.snippet(caller.file_path, max(1, line - 3), line + 3) if caller else ""
    authority_match = re.search(r"['\"`]([A-Za-z0-9_.-]+:\d{2,6})['\"`]", text or "")
    authority = authority_match.group(1) if authority_match else None
    fully_qualified = method if method.startswith("/") else None
    if fully_qualified is None and "." in qualified:
        pieces = qualified.replace("::", ".").split(".")
        if len(pieces) >= 3:
            fully_qualified = "/" + ".".join(pieces[:-2]) + "." + pieces[-2] + "/" + pieces[-1]
    package, service = _grpc_package_service(fully_qualified)
    return {
        "rpc": {
            "package": package,
            "service": service,
            "method": method,
            "fully_qualified_method": fully_qualified,
            "authority": authority,
            "streaming": "streaming" if any(token in method.lower() for token in ("stream", "watch")) else "unary",
            "identity_resolution": "generated_stub" if "stub" in qualified.lower() or fully_qualified else "callee_name",
        }
    }


def _grpc_package_service(fully_qualified):
    value = str(fully_qualified or "")
    if not value.startswith("/") or "/" not in value[1:]:
        return None, None
    service_name = value[1:].split("/", 1)[0]
    if "." not in service_name:
        return None, service_name
    package, service = service_name.rsplit(".", 1)
    return package or None, service or None


def _resource_type(name):
    lower = name.lower()
    return "redis" if "redis" in lower else "sql" if any(token in lower for token in ("query", "execute", "select", "insert", "update")) else "resource"


def _resource_access_mode(operation):
    lower = str(operation or "").lower()
    if any(token in lower for token in ("insert", "update", "delete", "put", "write", "set", "publish", "xadd")):
        return "write"
    if any(token in lower for token in ("query", "select", "get", "read", "fetch", "find", "scan", "xread")):
        return "read"
    return "unknown"


def _looks_like_resource(row, operation):
    text = " ".join(str(row.get(key) or "") for key in ("name", "target", "signature", "metadata", "callee_name", "function", "file_path", "operation")).lower()
    return any(token in text for token in (
        "redis", "cache", "query", "execute", "select", "insert", "update", "delete",
        "dynamo", "mongo", "postgres", "mysql", "sqlite", "sqlalchemy", "jdbc", "database",
        "transaction", "session", "get_item", "put_item", "collection", "keyvalue",
    ))


def _is_message_operation(name: str) -> bool:
    lower = name.lower()
    return any(
        token in lower
        for token in ("publish", "subscribe", "xadd", "xread", "xreadgroup", "basic_publish")
    )


def _unique(values):
    seen = set()
    result = []
    for value in values:
        if value.observation_id in seen:
            continue
        seen.add(value.observation_id)
        result.append(value)
    return result


__all__ = ["CommunicationFactExtractor"]
