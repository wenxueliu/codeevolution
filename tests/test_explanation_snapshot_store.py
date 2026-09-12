import sqlite3

import pytest

from codeevolution.domain.explanation import (
    ExplanationChunk,
    ExplanationEdge,
    ExplanationNode,
    ExplanationSnapshot,
)
from codeevolution.infrastructure.explanation_snapshot_store import (
    ExplanationSnapshotStore,
    SnapshotStateError,
)


def snapshot(id_: str, *, status: str = "running", created_at: int = 1) -> ExplanationSnapshot:
    return ExplanationSnapshot(
        id=id_,
        repo_name="shop",
        member_name="orders",
        api_key="POST /orders::create_order",
        method="POST",
        path="/orders",
        handler="create_order",
        entry_node_key="orders::create_order",
        source_revision="abc123",
        source_digest=f"source-{id_}",
        graph_digest=f"graph-{id_}",
        model_id="test-model",
        prompt_version="local-v1/aggregate-v1",
        schema_version="v1",
        status=status,
        created_at=created_at,
    )


@pytest.fixture
def store(tmp_path):
    value = ExplanationSnapshotStore(tmp_path / "explanations.db")
    yield value
    value.close()


def test_saves_and_reads_complete_snapshot_graph(store):
    saved = store.create_snapshot(snapshot("one"))
    assert saved.created_at == 1

    node = ExplanationNode(
        snapshot_id="one",
        node_key="orders::create_order",
        source_hash="node-source",
        local_digest="local",
        aggregate_digest="aggregate",
        local_explanation={"summary": "校验订单"},
        aggregate_explanation={"summary": "创建订单"},
        status="completed",
    )
    store.save_node(node)
    chunk = ExplanationChunk(
        snapshot_id="one",
        node_key=node.node_key,
        chunk_index=0,
        line_start=10,
        line_end=24,
        source_hash="chunk-source",
        explanation={"business_rules": [{"text": "数量必须为正数"}]},
        status="completed",
    )
    store.save_chunk(chunk)
    edge = ExplanationEdge(
        snapshot_id="one",
        caller_key=node.node_key,
        callee_key="orders::save_order",
        call_site={"line": 20},
        call_context={"transaction": "order"},
    )
    store.save_edge(edge)

    assert store.get_node("one", node.node_key) == node
    assert store.list_nodes("one") == [node]
    assert store.list_chunks("one", node.node_key) == [chunk]
    assert store.list_edges("one") == [edge]

    completed = store.update_snapshot(
        "one",
        "completed",
        explanation={"summary": "创建订单并持久化"},
        coverage={"total_nodes": 1, "completed_nodes": 1},
        statistics={"model_calls": 1},
    )
    assert completed.completed_at is not None
    assert completed.explanation["summary"] == "创建订单并持久化"

    with pytest.raises(SnapshotStateError, match="immutable"):
        store.save_node(node)
    with pytest.raises(SnapshotStateError, match="immutable"):
        store.update_snapshot("one", "failed")


def test_prompt_profile_persists_all_editable_templates(store):
    profile = store.create_prompt_profile(
        "repository-1",
        "重点关注状态变化",
        prompt_templates={
            "local": "local template",
            "synthesis": "synthesis template",
            "aggregate": "aggregate template",
        },
    )
    loaded = store.get_prompt_profile(profile["id"])
    assert loaded["prompt_text"] == "重点关注状态变化"
    assert loaded["prompt_templates"] == {
        "local": "local template",
        "synthesis": "synthesis template",
        "aggregate": "aggregate template",
    }


def test_migrates_legacy_snapshot_prompt_columns(tmp_path):
    path = tmp_path / "legacy-explanations.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE explanation_snapshots (
            id TEXT PRIMARY KEY,
            repo_name TEXT NOT NULL,
            member_name TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            handler TEXT NOT NULL,
            entry_node_key TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            graph_digest TEXT NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            status TEXT NOT NULL,
            explanation TEXT NOT NULL DEFAULT '{}',
            coverage TEXT NOT NULL DEFAULT '{}',
            statistics TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            completed_at INTEGER
        )"""
    )
    connection.commit()
    connection.close()

    migrated = ExplanationSnapshotStore(path)
    columns = {
        row["name"]
        for row in migrated.connection.execute("PRAGMA table_info(explanation_snapshots)")
    }
    assert {"repository_snapshot_id", "prompt_profile_id", "prompt_digest"} <= columns

    saved = migrated.create_snapshot(snapshot("legacy", created_at=2))
    assert saved.prompt_profile_id == ""
    assert saved.prompt_digest == ""
    migrated.close()


def test_publish_atomically_switches_endpoint_current_pointer(store):
    store.create_snapshot(snapshot("old", created_at=1))
    store.update_snapshot("old", "completed")
    store.publish("old")
    assert store.get_current("shop", "orders", "POST /orders::create_order").id == "old"

    store.create_snapshot(snapshot("candidate", created_at=2))
    with pytest.raises(SnapshotStateError, match="cannot be published"):
        store.publish("candidate")
    assert store.get_current("shop", "orders", "POST /orders::create_order").id == "old"

    store.update_snapshot("candidate", "partial", coverage={"partial_nodes": 1})
    store.publish("candidate")
    assert store.get_current("shop", "orders", "POST /orders::create_order").id == "candidate"
    assert [item.id for item in store.list_snapshots("shop", "orders")] == ["candidate", "old"]


def test_delete_current_requires_confirmation_and_does_not_fall_back(store):
    for id_, created in (("old", 1), ("current", 2)):
        store.create_snapshot(snapshot(id_, created_at=created))
        store.update_snapshot(id_, "completed")
        store.publish(id_)

    with pytest.raises(SnapshotStateError, match="explicit confirmation"):
        store.delete("current")
    store.delete("current", confirm_current=True)

    assert store.get_snapshot("current") is None
    assert store.get_current("shop", "orders", "POST /orders::create_order") is None
    assert store.get_snapshot("old") is not None


def test_running_snapshot_must_be_cancelled_before_delete(store):
    store.create_snapshot(snapshot("run"))
    with pytest.raises(SnapshotStateError, match="must be cancelled"):
        store.delete("run")

    cancelled = store.cancel("run")
    assert cancelled.status == "cancelled"
    assert store.is_cancelled("run")
    store.delete("run")
    assert store.get_snapshot("run") is None


def test_delete_cascades_graph_rows(store):
    store.create_snapshot(snapshot("cascade"))
    store.save_node(
        ExplanationNode("cascade", "node", "s", "l", "a", status="completed")
    )
    store.save_chunk(ExplanationChunk("cascade", "node", 0, 1, 2, "s"))
    store.save_edge(ExplanationEdge("cascade", "node", "child"))
    store.cancel("cascade")
    store.delete("cascade")

    counts = {
        table: store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "explanation_snapshot_nodes",
            "explanation_snapshot_chunks",
            "explanation_snapshot_edges",
        )
    }
    assert counts == {name: 0 for name in counts}


def test_missing_parent_and_invalid_chunk_ranges_are_rejected(store):
    with pytest.raises(KeyError):
        store.save_node(ExplanationNode("missing", "node", "s", "l", "a"))
    with pytest.raises(ValueError, match="valid inclusive line range"):
        store.save_chunk(ExplanationChunk("missing", "node", 0, 5, 4, "s"))
