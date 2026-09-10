"""Snapshot-bound static communication fact extraction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from codeevolution.analysis.communication.entry_collector import collect_entries, reachable_call_paths
from codeevolution.analysis.communication.schema import (
    CollectorCoverage,
    CollectorResult,
    CollectorRuleSet,
    CollectorStatus,
    CommunicationObservation,
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


class CommunicationFactExtractor:
    """Extract target-independent observations from frozen graph/source ports."""

    def collect(
        self,
        graph,
        sources,
        rules: CollectorRuleSet,
        *,
        snapshot_id: str,
    ) -> RepositoryCommunicationArtifact:
        entries = collect_entries(graph, snapshot_id, max_entries=rules.budgets.get("entries", 5000))
        paths = reachable_call_paths(
            graph,
            entries,
            max_depth=rules.budgets.get("call_depth", 12),
            max_nodes=rules.budgets.get("call_nodes", 10000),
        )
        functions = {item.node_id: item for item in graph.functions()}
        result = [
            self._http_result(graph, sources, entries, paths, functions, rules),
            self._message_result(graph, sources, entries, paths, functions, rules),
            self._grpc_result(graph, sources, entries, paths, functions, rules),
            self._resource_result(graph, sources, entries, paths, functions, rules),
        ]
        return build_communication_artifact(snapshot_id=snapshot_id, rules_digest=rules.digest, results=result)

    def _http_result(self, graph, sources, entries, paths, functions, rules):
        observations = []
        unresolved = []
        entry_by_id = {entry.entry_id: entry for entry in entries}
        for pattern in HTTP_PATTERNS:
            for row in graph.http_client_calls(pattern):
                caller_id = row.get("caller_node_id")
                entry = _entry_for_node(caller_id, paths, entry_by_id)
                if entry is None:
                    continue
                caller = functions.get(caller_id)
                call_line = int(row.get("call_line") or row.get("caller_line") or 1)
                payload = _http_payload(sources, caller, call_line, row.get("callee_name", ""))
                observation = self._observation(
                    "http", entry, caller, call_line, payload, row, extraction_confidence=0.95 if payload.get("request", {}).get("raw_url") else 0.45
                )
                if payload.get("request", {}).get("raw_url"):
                    observations.append(observation)
                else:
                    unresolved.append(observation)
        return CollectorResult(
            CollectorCoverage("http", "http-collector/v1", CollectorStatus.COMPLETE, coverage={"entries": len(entries)}),
            entries=entries,
            http_outbounds=tuple(_unique(observations)), unresolved_observations=tuple(_unique(unresolved)),
        )

    def _message_result(self, graph, sources, entries, paths, functions, rules):
        publications, subscriptions, unresolved = [], [], []
        for pattern in MQ_PATTERNS:
            for row in graph.mq_producer_calls(pattern):
                entry = _entry_for_node(row.get("caller_node_id"), paths, {item.entry_id: item for item in entries})
                if entry is None:
                    continue
                caller = functions.get(row.get("caller_node_id"))
                call_line = int(row.get("call_line") or row.get("start_line") or 1)
                channel = _channel(sources, caller, call_line)
                observation = self._observation(
                    "message", entry, caller, call_line,
                    {"messaging": {"protocol": _broker(row.get("callee_name", "")), "channel": channel, "delivery_semantics": "unknown"}},
                    row, extraction_confidence=0.9 if channel else 0.35,
                )
                (publications if channel else unresolved).append(observation)
            for row in graph.mq_consumers(pattern):
                if not row.get("node_id"):
                    continue
                entry = _entry_for_node(row["node_id"], paths, {item.entry_id: item for item in entries})
                if entry is None:
                    # A consumer is itself a production entry even when the
                    # graph adapter did not classify the framework decorator.
                    continue
                caller = functions.get(row["node_id"])
                channel = _channel(sources, caller, int(row.get("start_line") or 1))
                observation = self._observation(
                    "message", entry, caller, int(row.get("start_line") or 1),
                    {"messaging": {"protocol": _broker(row.get("name", "")), "channel": channel, "delivery_semantics": "unknown"}},
                    row, extraction_confidence=0.7 if channel else 0.3,
                )
                (subscriptions if channel else unresolved).append(observation)
        return CollectorResult(
            CollectorCoverage("message", "message-collector/v1", CollectorStatus.COMPLETE),
            message_publications=tuple(_unique(publications)), message_subscriptions=tuple(_unique(subscriptions)),
            unresolved_observations=tuple(_unique(unresolved)),
        )

    def _grpc_result(self, graph, sources, entries, paths, functions, rules):
        clients, unresolved = [], []
        for pattern in RPC_PATTERNS:
            for row in graph.rpc_calls(pattern):
                caller = functions.get(row.get("caller_node_id"))
                entry = _entry_for_node(row.get("caller_node_id"), paths, {item.entry_id: item for item in entries})
                if entry is None:
                    continue
                call_line = int(row.get("call_line") or row.get("start_line") or 1)
                rpc = str(row.get("callee_name") or "")
                observation = self._observation(
                    "grpc", entry, caller, call_line,
                    {"rpc": {"method": rpc, "identity_resolution": "callee_name"}}, row,
                    extraction_confidence=0.65,
                )
                clients.append(observation)
        return CollectorResult(
            CollectorCoverage("grpc", "grpc-collector/v1", CollectorStatus.COMPLETE),
            grpc_clients=tuple(_unique(clients)), unresolved_observations=tuple(_unique(unresolved)),
        )

    def _resource_result(self, graph, sources, entries, paths, functions, rules):
        accesses = []
        for row in graph.database_call_candidates():
            caller_id = row.get("caller_node_id")
            entry = _entry_for_node(caller_id, paths, {item.entry_id: item for item in entries})
            if entry is None:
                continue
            caller = functions.get(caller_id)
            operation = str(row.get("name") or row.get("target") or "unknown")
            accesses.append(
                self._observation(
                    "resource", entry, caller, int(row.get("call_line") or row.get("start_line") or 1),
                    {"resource": {"type": _resource_type(operation), "operation": operation, "instance_id": "unresolved"}}, row,
                    extraction_confidence=0.55,
                )
            )
        return CollectorResult(
            CollectorCoverage("resource", "resource-collector/v1", CollectorStatus.COMPLETE),
            resource_accesses=tuple(_unique(accesses)),
        )

    @staticmethod
    def _observation(kind, entry, caller, call_line, payload, row, extraction_confidence):
        caller_ref = None
        callsite = None
        if caller is not None:
            caller_ref = NodeRef(
                snapshot_id=entry.handler.snapshot_id,
                node_id=caller.node_id,
                kind=caller.kind,
                name=caller.name,
                qualified_name=caller.qualified_name,
                location=Location(caller.file_path, max(1, caller.start_line)),
            )
            callsite = Location(caller.file_path, max(1, call_line))
        observation_id = f"{kind}:{entry.entry_id}:{row.get('caller_node_id', '')}:{call_line}:{row.get('callee_node_id', '')}"
        return CommunicationObservation(observation_id, entry.entry_id, caller_ref, callsite, payload, extraction_confidence)


def _entry_for_node(node_id, paths, entries):
    for entry_id, path_map in paths.items():
        if node_id in path_map:
            return entries.get(entry_id)
    return None


def _http_payload(sources, caller, call_line, callee_name):
    text = sources.snippet(caller.file_path, max(1, call_line - 2), call_line + 2) if caller else None
    raw = _first_url(text or "")
    method = _method(callee_name)
    if raw.startswith("http"):
        parsed = urlsplit(raw)
        authority = parsed.netloc
        path = parsed.path or "/"
    else:
        authority, path = None, raw if raw.startswith("/") else None
    return {"request": {"method": method, "raw_url": raw or None, "authority": authority, "normalized_path": path}}


def _channel(sources, caller, line):
    if caller is None:
        return ""
    text = sources.snippet(caller.file_path, max(1, line - 2), line + 2) or ""
    match = re.search(r"['\"`]([A-Za-z0-9_.:/-]{2,})['\"`]", text)
    return match.group(1) if match else ""


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


def _resource_type(name):
    lower = name.lower()
    return "redis" if "redis" in lower else "sql" if any(token in lower for token in ("query", "execute", "select", "insert", "update")) else "resource"


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
