from types import SimpleNamespace

from codehistory.analysis.topology.cross_repo_impl import CrossServiceEdge, UnifiedTopology
from codehistory.application.evolution_service import EvolutionQueryService
from codehistory.application.runtime_telemetry_service import RuntimeTelemetryService
from codehistory.application.topology_service import TopologyService
from codehistory.infrastructure.otlp_json import OTLPJSONCollector
from codehistory.store import EvolutionStore


def _edge(source, target, path):
    return CrossServiceEdge(
        source,
        "caller",
        "client.py",
        2,
        target,
        "handler",
        "routes.py",
        1,
        "GET",
        path,
        f"http://{target}{path}",
    )


def test_runtime_spans_confirm_and_extend_static_topology():
    topology = UnifiedTopology(
        services=[],
        cross_edges=[_edge("gateway", "orders", "/orders/:id"), _edge("orders", "users", "/users")],
    )
    service = TopologyService(SimpleNamespace(build=lambda: topology))
    result = service.validate_runtime(
        [
            {
                "source_service": "gateway",
                "target_service": "orders",
                "method": "GET",
                "path": "/orders/42",
                "latency_ms": 10,
                "trace_id": "trace-1",
            },
            {
                "source_service": "gateway",
                "target_service": "orders",
                "method": "GET",
                "path": "/orders/42",
                "latency_ms": 20,
                "trace_id": "trace-2",
            },
            {
                "source_service": "orders",
                "target_service": "payments",
                "method": "POST",
                "path": "/charge",
                "latency_ms": 5,
            },
        ]
    )

    assert result["summary"] == {"confirmed": 1, "static_only": 1, "runtime_only": 1}
    assert result["confirmed"][0]["runtime_count"] == 2
    assert result["confirmed"][0]["average_latency_ms"] == 15
    assert result["runtime_only"][0]["target_service"] == "payments"


def test_otlp_ingest_persists_and_associates_spans_logs_and_errors(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    commit_id = store.insert_commit("abc", None, 1, "tester", "fixture")
    feature_id = store.insert_feature(
        "api.py::order", "Order", "http", "GET /orders/:id", commit_id
    )
    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "gateway"}}
            ]},
            "scopeSpans": [{"spans": [{
                "traceId": "trace-1", "spanId": "span-1",
                "startTimeUnixNano": "1000000", "endTimeUnixNano": "6000000",
                "attributes": [
                    {"key": "http.request.method", "value": {"stringValue": "GET"}},
                    {"key": "url.path", "value": {"stringValue": "/orders/42"}},
                    {"key": "peer.service", "value": {"stringValue": "orders"}},
                ],
                "events": [{"name": "exception", "timeUnixNano": "5000000", "attributes": [
                    {"key": "exception.type", "value": {"stringValue": "TimeoutError"}},
                    {"key": "exception.message", "value": {"stringValue": "slow"}},
                ]}],
            }]}],
        }],
        "resourceLogs": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "gateway"}}
            ]},
            "scopeLogs": [{"logRecords": [{
                "traceId": "trace-1", "spanId": "span-1", "timeUnixNano": "4000000",
                "severityText": "INFO", "body": {"stringValue": "request started"},
            }]}],
        }],
    }

    result = RuntimeTelemetryService(OTLPJSONCollector(), store).ingest(payload)

    assert result["summary"] == {
        "total": 3, "spans": 1, "logs": 1, "errors": 1, "associated": 3
    }
    saved = store.list_runtime_observations(feature_id=feature_id)
    assert {item["kind"] for item in saved} == {"span", "log", "error"}
    assert next(item for item in saved if item["kind"] == "span")["latency_ms"] == 5
    assert next(item for item in saved if item["kind"] == "error")["error_type"] == "TimeoutError"
    detail = EvolutionQueryService(store).feature_detail("api.py::order")
    assert len(detail["runtime_observations"]) == 3


def test_otlp_explicit_feature_attribute_and_runtime_validation(tmp_path):
    store = EvolutionStore(str(tmp_path / "evolution.db"))
    commit_id = store.insert_commit("abc", None, 1, "tester", "fixture")
    feature_id = store.insert_feature("worker::run", "Worker", "event", "orders", commit_id)
    topology = UnifiedTopology(services=[], cross_edges=[_edge("gateway", "orders", "/orders")])
    topology_service = TopologyService(SimpleNamespace(build=lambda: topology))
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "trace-2", "startTimeUnixNano": "1", "endTimeUnixNano": "2",
        "attributes": [
            {"key": "service.name", "value": {"stringValue": "gateway"}},
            {"key": "peer.service", "value": {"stringValue": "orders"}},
            {"key": "http.request.method", "value": {"stringValue": "GET"}},
            {"key": "url.path", "value": {"stringValue": "/orders"}},
            {"key": "codehistory.feature.stable_id", "value": {"stringValue": "worker::run"}},
        ],
    }]}]}]}

    result = RuntimeTelemetryService(OTLPJSONCollector(), store, topology_service).ingest(payload)

    assert result["observations"][0]["feature_id"] == feature_id
    assert result["topology_validation"]["summary"]["confirmed"] == 1
