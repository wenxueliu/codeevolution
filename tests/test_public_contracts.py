"""Characterization tests for public delivery and import contracts."""

import dataclasses
import inspect
import json
import subprocess
import sys

from codehistory import codegraph_reader, cross_repo, knowledge, mcp_server, p2_advanced
from codehistory.api import app


def test_legacy_dto_contracts_are_stable():
    expected = {
        codegraph_reader.FunctionDef: [
            "node_id",
            "name",
            "qualified_name",
            "file_path",
            "language",
            "start_line",
            "end_line",
            "kind",
            "signature",
            "visibility",
            "is_exported",
            "is_async",
            "is_static",
            "is_test",
            "parent_class",
            "decorators",
        ],
        knowledge.ApiEndpoint: [
            "method",
            "path",
            "handler_name",
            "file_path",
            "line",
            "params",
            "return_type",
                "decorators",
                "downstream_calls",
                "request_headers",
                "query_params",
                "path_params",
                "request_body",
                "response_body",
                "call_chain",
                "frontend_callers",
        ],
        cross_repo.UnifiedTopology: [
            "services",
            "cross_edges",
            "dependency_graph",
            "potential_edges",
        ],
        p2_advanced.FlowDiagram: [
            "entry_service",
            "entry_api",
            "steps",
            "services_involved",
            "total_cross_service_calls",
            "channels_used",
        ],
    }
    for dto, field_names in expected.items():
        assert [field.name for field in dataclasses.fields(dto)] == field_names


def test_cli_help_lists_all_public_commands():
    result = subprocess.run(
        [sys.executable, "-m", "codehistory.cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    commands = {
        "backfill",
        "update",
        "serve",
        "web",
        "register",
        "repos",
        "status",
        "knowledge",
        "topology",
        "impact",
        "trace",
        "discover",
        "check",
        "init-all",
        "flow",
        "entities",
    }
    words = result.stdout.replace("{", " ").replace("}", " ").replace(",", " ").split()
    assert commands <= set(words)
    assert result.stderr == ""


def test_openapi_path_snapshot():
    schema = app.openapi()
    assert schema["info"]["title"] == "CodeHistory API"
    assert sorted(schema["paths"]) == [
        "/api/audit-logs",
        "/api/capabilities",
        "/api/chat",
        "/api/commits",
        "/api/event-stats",
        "/api/events",
        "/api/features",
        "/api/features/{stable_id}",
        "/api/features/{stable_id}/explain",
        "/api/knowledge",
        "/api/llm-status",
        "/api/repos",
        "/api/repos/register",
        "/api/repos/{name}",
        "/api/stats",
    ]


def test_mcp_tool_function_contracts():
    expected = {
        "get_feature_timeline": ["feature_name"],
        "list_features": [],
        "get_stats": [],
        "search_feature_history": ["query"],
        "get_feature_summary": ["feature_name"],
    }
    for name, params in expected.items():
        function = getattr(mcp_server, name)
        assert list(inspect.signature(function).parameters) == params
        assert json.loads(function(**{param: "x" for param in params})) == {
            "error": "No store configured"
        }


def test_matcher_characterization_tables():
    analyzer = p2_advanced.P2Analyzer([])
    assert analyzer._topics_match("orders.created", "orders.created")
    assert not analyzer._topics_match("orders.*", "orders.created")
    assert not analyzer._topics_match("orders.created", "payments.created")
    assert analyzer._entity_similarity("UserService", "UserSvc") >= 0.8
    assert analyzer._entity_similarity("Order", "Payment") < 0.5
