import hashlib

import pytest

from codeevolution.analysis.knowledge.explanation_graph import build_explanation_plan
from codeevolution.analysis.knowledge.explanation_validation import validate_explanation_coverage
from codeevolution.analysis.knowledge.source_chunking import chunk_source


def test_graph_plan_is_leaf_first_and_reports_shared_nodes():
    plan = build_explanation_plan(
        [{"node_key": key} for key in ("entry", "left", "right", "shared")],
        [
            {"caller": "entry", "callee": "left"},
            {"caller": "entry", "callee": "right"},
            {"caller": "left", "callee": "shared"},
            {"caller": "right", "callee": "shared"},
        ],
    )

    positions = {node: index for index, node in enumerate(plan.node_order)}
    assert positions["shared"] < positions["left"] < positions["entry"]
    assert positions["shared"] < positions["right"] < positions["entry"]
    assert plan.shared_nodes == ("shared",)
    assert len(plan.node_keys) == 4


def test_graph_plan_condenses_cycles_and_marks_self_recursion():
    plan = build_explanation_plan(
        ["entry", "a", "b", "recursive"],
        [("entry", "a"), ("a", "b"), ("b", "a"), ("recursive", "recursive")],
    )

    cycle = next(component for component in plan.components if set(component.node_keys) == {"a", "b"})
    recursive = next(component for component in plan.components if component.node_keys == ("recursive",))
    assert cycle.cyclic is True
    assert recursive.cyclic is True
    assert plan.node_order.index("a") < plan.node_order.index("entry")
    assert plan.node_order.index("b") < plan.node_order.index("entry")


def test_graph_plan_retains_edge_only_nodes_and_truncation_reason():
    plan = build_explanation_plan([], [("caller", "missing-child")], truncation_reasons=["node cap reached"])
    assert plan.node_keys == ("caller", "missing-child")
    assert plan.truncated is True
    assert plan.truncation_reasons == ("node cap reached",)


def test_source_chunking_is_lossless_line_addressed_and_hashed():
    source = "".join(f"line {number}\n" for number in range(1, 14))
    result = chunk_source(source, line_start=40, max_lines=5, overlap_lines=1)

    assert len(result.chunks) == 3
    assert result.uncovered_lines == ()
    assert result.chunks[0].line_start == 40
    assert result.chunks[-1].line_end == 52
    assert all(chunk.line_end - chunk.line_start + 1 <= 5 for chunk in result.chunks)
    assert result.source_hash == hashlib.sha256(source.encode()).hexdigest()
    assert all(chunk.source_hash == hashlib.sha256(chunk.source.encode()).hexdigest() for chunk in result.chunks)


def test_source_chunking_marks_provider_or_declared_truncation():
    result = chunk_source(
        "one\ntwo\n",
        line_start=10,
        expected_line_end=14,
        source_complete=False,
        max_lines=2,
        overlap_lines=0,
    )
    assert result.source_complete is False
    assert result.truncated is True
    assert "expected through line 14" in result.truncation_reason
    assert result.uncovered_lines == ()  # supplied text itself was never dropped


@pytest.mark.parametrize(
    ("max_lines", "overlap"),
    [(0, 0), (5, -1), (5, 5)],
)
def test_source_chunking_rejects_unsafe_sizes(max_lines, overlap):
    with pytest.raises(ValueError):
        chunk_source("x", max_lines=max_lines, overlap_lines=overlap)


def _complete_node(key, start=1, end=2):
    return {
        "node_key": key,
        "status": "completed",
        "line_start": start,
        "line_end": end,
        "source_complete": True,
        "chunks_total": 1,
        "local_explanation": {"summary": key, "side_effects": ["write"]},
        "aggregate_explanation": {"summary": key, "side_effects": ["write"]},
    }


def test_coverage_validation_accepts_complete_traceable_snapshot():
    text = "a\nb\n"
    coverage = validate_explanation_coverage(
        [_complete_node("entry"), {"node_key": "external", "type": "external"}],
        [{
            "node_key": "entry",
            "chunk_index": 0,
            "line_start": 1,
            "line_end": 2,
            "source": text,
            "source_hash": hashlib.sha256(text.encode()).hexdigest(),
            "status": "completed",
        }],
        [{"caller": "entry", "callee": "external"}],
        {"entry_node_key": "entry", "summary": "does work"},
    )

    assert coverage.status == "completed"
    assert coverage.completed_nodes == 1
    assert coverage.unresolved_external_nodes == 1
    assert coverage.issues == ()


def test_coverage_validation_exposes_gaps_failures_and_truncation():
    node = _complete_node("entry", end=4)
    node["source_complete"] = False
    node["aggregate_explanation"] = {"summary": "entry"}
    coverage = validate_explanation_coverage(
        [node],
        [{
            "node_key": "entry",
            "chunk_index": 0,
            "line_start": 1,
            "line_end": 2,
            "source_hash": "bad",
            "status": "failed",
        }],
        api_explanation={"entry_node_key": "missing"},
        graph_truncated=True,
        truncation_reasons=["child cap reached"],
    )

    codes = {issue.code for issue in coverage.issues}
    assert coverage.status == "partial"
    assert coverage.truncated is True
    assert {
        "source_incomplete",
        "aggregate_fact_missing",
        "chunk_incomplete",
        "chunk_line_gap",
        "entry_node_unknown",
        "api_summary_missing",
        "analysis_truncated",
    } <= codes
