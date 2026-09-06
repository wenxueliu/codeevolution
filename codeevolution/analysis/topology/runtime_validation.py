"""Reconcile static topology edges with normalized OpenTelemetry span edges."""

from .matching import PathMatcher


class RuntimeTopologyValidator:
    def validate(self, topology, spans: list[dict]) -> dict:
        runtime = self._aggregate(spans)
        confirmed, static_only = [], []
        matched_runtime: set[int] = set()
        for edge in topology.cross_edges:
            match_index = next(
                (
                    index
                    for index, item in enumerate(runtime)
                    if index not in matched_runtime
                    and item["source_service"] == edge.source_service
                    and item["target_service"] == edge.target_service
                    and self._method_matches(item["method"], edge.http_method)
                    and PathMatcher.matches(item["path"], edge.url_pattern)
                ),
                None,
            )
            static = self._static_edge(edge)
            if match_index is None:
                static_only.append(static)
                continue
            matched_runtime.add(match_index)
            observed = runtime[match_index]
            confirmed.append(
                {
                    **static,
                    "runtime_count": observed["count"],
                    "average_latency_ms": observed["average_latency_ms"],
                    "confidence": max(edge.confidence, 0.95),
                }
            )
        runtime_only = [item for index, item in enumerate(runtime) if index not in matched_runtime]
        return {
            "confirmed": confirmed,
            "static_only": static_only,
            "runtime_only": runtime_only,
            "summary": {
                "confirmed": len(confirmed),
                "static_only": len(static_only),
                "runtime_only": len(runtime_only),
            },
        }

    @staticmethod
    def _aggregate(spans: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str, str, str], dict] = {}
        for span in spans:
            if span.get("kind", "span") != "span":
                continue
            key = (
                str(span.get("source_service") or ""),
                str(span.get("target_service") or ""),
                str(span.get("method") or "").upper(),
                str(span.get("path") or "/"),
            )
            item = groups.setdefault(
                key,
                {
                    "source_service": key[0],
                    "target_service": key[1],
                    "method": key[2],
                    "path": key[3],
                    "count": 0,
                    "total_latency_ms": 0.0,
                    "evidence": [],
                },
            )
            count = max(int(span.get("count") or 1), 1)
            item["count"] += count
            item["total_latency_ms"] += float(span.get("latency_ms") or 0) * count
            if span.get("trace_id"):
                item["evidence"].append({"trace_id": span["trace_id"]})
        result = []
        for item in groups.values():
            total = item.pop("total_latency_ms")
            item["average_latency_ms"] = round(total / item["count"], 3)
            result.append(item)
        return result

    @staticmethod
    def _method_matches(runtime_method: str, static_method: str) -> bool:
        return not runtime_method or not static_method or runtime_method == static_method.upper()

    @staticmethod
    def _static_edge(edge) -> dict:
        return {
            "source_service": edge.source_service,
            "target_service": edge.target_service,
            "method": edge.http_method,
            "path": edge.url_pattern,
            "confidence": edge.confidence,
            "evidence": edge.evidence,
            "rule_version": edge.rule_version,
        }
