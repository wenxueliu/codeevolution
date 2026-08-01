from types import SimpleNamespace

from codehistory.analysis.topology.cross_repo_impl import CrossServiceEdge, UnifiedTopology
from codehistory.application.topology_service import TopologyService


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
