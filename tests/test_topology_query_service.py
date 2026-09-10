import pytest

from codeevolution.application.topology_query_service import (
    TopologyQueryError,
    TopologyQueryService,
)


def _artifact():
    return {
        "services": [
            {"member_id": "gateway", "entries": [{"entry_id": "gateway.root", "method": "GET", "path_template": "/"}]},
            {"member_id": "orders", "entries": [{"entry_id": "orders.root", "method": "GET", "path_template": "/orders"}]},
            {"member_id": "users", "entries": [{"entry_id": "users.root", "method": "GET", "path_template": "/users"}]},
        ],
        "service_projections": [
            {"kind": "http", "source_member_id": "gateway", "target_member_id": "orders"},
            {"kind": "http", "source_member_id": "orders", "target_member_id": "users"},
        ],
        "endpoint_dependencies": [
            {"kind": "http", "edge_id": "e1", "source": {"member_id": "gateway", "entry_id": "gateway.root"}, "target": {"member_id": "orders", "entry_ids": ["orders.root"]}},
            {"kind": "http", "edge_id": "e2", "source": {"member_id": "orders", "entry_id": "orders.root"}, "target": {"member_id": "users", "entry_ids": ["users.root"]}},
        ],
        "resource_dependencies": [],
        "coverage": {"status": "complete", "unknown_boundaries": []},
        "candidates": [],
    }


def test_impact_returns_both_direction_transitive_paths():
    result = TopologyQueryService().impact(_artifact(), "orders")

    assert result["downstream_dependencies"] == [{"member_id": "users", "path": ["orders", "users"], "depth": 1}]
    assert result["upstream_dependents"] == [{"member_id": "gateway", "path": ["orders", "gateway"], "depth": 1}]


def test_flow_returns_rooted_graph_and_cycle_reference():
    artifact = _artifact()
    artifact["endpoint_dependencies"].append(
        {"kind": "http", "edge_id": "e3", "source": {"member_id": "users", "entry_id": "users.root"}, "target": {"member_id": "gateway", "entry_ids": ["gateway.root"]}}
    )

    result = TopologyQueryService().flow(artifact, "gateway", entry_id="gateway.root")

    assert result["root"] == {"member_id": "gateway", "entry_id": "gateway.root"}
    assert len(result["nodes"]) == 3
    assert result["cycles"]
    assert result["truncation"] is None


def test_flow_method_path_requires_unique_entry():
    with pytest.raises(TopologyQueryError, match="entry_not_in_view"):
        TopologyQueryService().flow(_artifact(), "gateway", method="POST", path="/")


def test_flow_rejects_limits_above_hard_cap():
    with pytest.raises(TopologyQueryError, match="flow_limits_exceeded"):
        TopologyQueryService().flow(_artifact(), "gateway", entry_id="gateway.root", max_nodes=5001)
