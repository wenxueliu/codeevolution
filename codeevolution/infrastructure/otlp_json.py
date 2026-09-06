"""Normalize OpenTelemetry OTLP JSON exports without requiring an SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue"):
        if key in value:
            raw = value[key]
            return int(raw) if key == "intValue" else raw
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return _attributes(value["kvlistValue"].get("values", []))
    return value


def _attributes(items: list[dict] | None) -> dict:
    return {item["key"]: _value(item.get("value")) for item in items or [] if item.get("key")}


def _body(value: Any) -> str:
    decoded = _value(value)
    if isinstance(decoded, str):
        return decoded
    return json.dumps(decoded, sort_keys=True) if decoded is not None else ""


class OTLPJSONCollector:
    """Collect normalized spans, logs, and span errors from an OTLP JSON payload."""

    def collect_file(self, path: str | Path) -> list[dict]:
        with Path(path).open(encoding="utf-8") as stream:
            return self.collect(json.load(stream))

    def collect(self, payload: dict) -> list[dict]:
        observations = []
        observations.extend(self._spans(payload.get("resourceSpans", [])))
        observations.extend(self._logs(payload.get("resourceLogs", [])))
        return observations

    def _spans(self, resources: list[dict]) -> list[dict]:
        result = []
        for group in resources:
            resource = _attributes(group.get("resource", {}).get("attributes"))
            service = str(resource.get("service.name") or "")
            scopes = group.get("scopeSpans") or group.get("instrumentationLibrarySpans") or []
            for scope in scopes:
                for span in scope.get("spans", []):
                    attrs = {**resource, **_attributes(span.get("attributes"))}
                    start = int(span.get("startTimeUnixNano") or 0)
                    end = int(span.get("endTimeUnixNano") or start)
                    observation = {
                        "kind": "span",
                        "timestamp_ns": start or None,
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "source_service": str(attrs.get("service.name") or service),
                        "target_service": str(
                            attrs.get("peer.service")
                            or attrs.get("server.address")
                            or attrs.get("network.peer.address")
                            or ""
                        ),
                        "method": str(
                            attrs.get("http.request.method") or attrs.get("http.method") or ""
                        ).upper(),
                        "path": str(
                            attrs.get("http.route")
                            or attrs.get("url.path")
                            or attrs.get("http.target")
                            or span.get("name")
                            or "/"
                        ),
                        "latency_ms": max(end - start, 0) / 1_000_000,
                        "attributes": attrs,
                    }
                    result.append(observation)
                    status = span.get("status", {})
                    is_error = status.get("code") in (2, "STATUS_CODE_ERROR", "ERROR")
                    if is_error:
                        result.append(
                            {
                                **observation,
                                "kind": "error",
                                "severity": "ERROR",
                                "message": status.get("message", ""),
                                "error_type": str(attrs.get("error.type") or "span.status"),
                            }
                        )
                    for event in span.get("events", []):
                        event_attrs = _attributes(event.get("attributes"))
                        if event.get("name") == "exception":
                            result.append(
                                {
                                    **observation,
                                    "kind": "error",
                                    "timestamp_ns": int(event.get("timeUnixNano") or start) or None,
                                    "severity": "ERROR",
                                    "message": str(event_attrs.get("exception.message") or ""),
                                    "error_type": str(event_attrs.get("exception.type") or "exception"),
                                    "attributes": {**attrs, **event_attrs},
                                }
                            )
        return result

    def _logs(self, resources: list[dict]) -> list[dict]:
        result = []
        for group in resources:
            resource = _attributes(group.get("resource", {}).get("attributes"))
            scopes = group.get("scopeLogs") or group.get("instrumentationLibraryLogs") or []
            for scope in scopes:
                for record in scope.get("logRecords", []):
                    attrs = {**resource, **_attributes(record.get("attributes"))}
                    severity = str(record.get("severityText") or "")
                    result.append(
                        {
                            "kind": "error" if severity.upper() in {"ERROR", "FATAL"} else "log",
                            "timestamp_ns": int(record.get("timeUnixNano") or 0) or None,
                            "trace_id": record.get("traceId", ""),
                            "span_id": record.get("spanId", ""),
                            "source_service": str(resource.get("service.name") or ""),
                            "severity": severity,
                            "message": _body(record.get("body")),
                            "error_type": str(attrs.get("error.type") or ""),
                            "attributes": attrs,
                        }
                    )
        return result
