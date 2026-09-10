"""CLI delivery tests for the Graph Artifact topology contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeevolution import cli


def _topology_args(**overrides):
    values = {
        "view_id": "view-1", "server": "https://codeevolution.test", "no_wait": False,
        "timeout": 1.0, "poll_interval": 0.01, "output": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_remote_topology_no_wait_prints_only_a_job_id(monkeypatch, capsys):
    calls = []

    def request(server, method, path, payload=None):
        calls.append((server, method, path, payload))
        return 202, {"job": {"id": "job-1", "status": "pending"}}, {"Retry-After": "2"}

    monkeypatch.setattr(cli, "_request_json", request)
    cli.cmd_topology(_topology_args(no_wait=True))

    assert capsys.readouterr().out == '{"job_id":"job-1","status":"pending","status_url":"/api/graph-artifact-jobs/job-1"}\n'
    assert calls == [
        ("https://codeevolution.test", "POST", "/api/graph-views/view-1/artifact-jobs",
         {"artifact_kind": "topology", "params": {}})
    ]


def test_impact_resolves_frozen_display_name_before_local_query(monkeypatch, capsys):
    artifact = {
        "services": [{"member_id": "gateway", "display_name": "Gateway"}, {"member_id": "orders", "display_name": "Orders"}],
        "service_projections": [{"kind": "http", "source_member_id": "gateway", "target_member_id": "orders"}],
        "resource_dependencies": [], "coverage": {"unknown_boundaries": []}, "candidates": [],
    }
    monkeypatch.setattr(cli, "_topology_for_query", lambda args: artifact)
    args = SimpleNamespace(
        view_id="view-1", server="", service="Gateway", direction="both", max_depth=5,
        channels="http,message,grpc", include_resources=True,
        include_shared_resource_risks=False, include_candidates=False, output="",
    )
    cli.cmd_impact(args)
    result = capsys.readouterr().out
    assert '"service":"gateway"' in result
    assert '"member_id":"orders"' in result


def test_flow_rejects_half_of_a_method_path_selector(capsys):
    args = SimpleNamespace(
        view_id="view-1", server="", service="gateway", entry_id="", method="GET", path="",
        max_depth=8, max_nodes=500, max_edges=1000, channels="http,message,grpc",
        include_resources=False, include_candidates=False, output="",
    )
    with pytest.raises(SystemExit) as raised:
        cli.cmd_flow(args)
    assert raised.value.code == 2
    assert "--method and --path" in capsys.readouterr().err


def test_topology_output_is_canonical_and_replaces_target_atomically(tmp_path):
    target = tmp_path / "topology.json"
    target.write_text("old", encoding="utf-8")
    cli._canonical_output({"b": 2, "a": 1}, str(target))
    assert target.read_text(encoding="utf-8") == '{"a":1,"b":2}\n'
    assert not list(tmp_path.glob(".topology.json.*"))
