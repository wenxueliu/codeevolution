from codeevolution.application.explanation_generation_service import (
    ExplanationGenerationService,
)
from codeevolution.infrastructure.explanation_snapshot_store import (
    ExplanationSnapshotStore,
)


class SourceStub:
    def load(self, spec):
        return {
            "repo": "shop",
            "member": "orders",
            "api_key": "POST|/orders|create_order",
            "entry_id": "root",
            "entry_node_key": "orders::create_order",
            "nodes": {
                "root": {
                    "id": "root", "node_key": "orders::create_order",
                    "name": "create_order", "qualified_name": "create_order",
                    "file": "orders.py", "line_start": 1, "line_end": 2,
                    "source": "def create_order():\n    return save_order()\n",
                    "source_hash": "root-hash",
                },
                "leaf": {
                    "id": "leaf", "node_key": "orders::save_order",
                    "name": "save_order", "qualified_name": "save_order",
                    "file": "orders.py", "line_start": 4, "line_end": 5,
                    "source": "def save_order():\n    return 1\n",
                    "source_hash": "leaf-hash",
                },
            },
            "edges": [{"source": "root", "target": "leaf", "call_line": 2}],
            "source_revision": "abc123", "source_digest": "source-digest",
            "graph_digest": "graph-digest", "truncated": False,
            "unresolved_external_nodes": 0,
        }


class SemanticStub:
    def __init__(self):
        self.local_order = []

    def explain_chunk(self, node, chunk):
        self.local_order.append(node["name"])
        return {
            "summary": f"{node['name']} local", "business_flow": [node["name"]],
            "business_rules": [], "state_changes": [], "side_effects": [],
            "exceptions": [],
        }

    def synthesize_local(self, node, chunks):
        return chunks[0]["explanation"]

    def aggregate_node(self, node, local, children):
        return {**local, "summary": f"{node['name']} aggregate", "children": children}


def test_generates_leaf_first_and_publishes_endpoint_snapshot(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "explanations.db")
    semantic = SemanticStub()
    service = ExplanationGenerationService(store, SourceStub(), semantic, "test-model")
    snapshot_id, frozen = service.prepare(
        {"repo": "shop", "member": "orders", "method": "POST",
         "path": "/orders", "handler": "create_order"}
    )

    service.generate(snapshot_id, frozen)

    snapshot = store.get_snapshot(snapshot_id)
    assert snapshot.status == "completed"
    assert semantic.local_order == ["save_order", "create_order"]
    assert snapshot.explanation["summary"] == "create_order aggregate"
    assert snapshot.coverage["completed_nodes"] == 2
    assert store.get_current("shop", "orders", snapshot.api_key).id == snapshot_id
    assert len(store.list_nodes(snapshot_id)) == 2
    store.close()


def test_regeneration_reuses_unchanged_node_and_chunk_explanations(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "explanations.db")
    source = SourceStub()
    first_semantic = SemanticStub()
    first = ExplanationGenerationService(store, source, first_semantic, "test-model")
    first_id, frozen = first.prepare(
        {"repo": "shop", "member": "orders", "method": "POST",
         "path": "/orders", "handler": "create_order"}
    )
    first.generate(first_id, frozen)

    second_semantic = SemanticStub()
    second = ExplanationGenerationService(store, source, second_semantic, "test-model")
    second_id, frozen = second.prepare(
        {"repo": "shop", "member": "orders", "method": "POST",
         "path": "/orders", "handler": "create_order"}
    )
    second.generate(second_id, frozen)

    snapshot = store.get_snapshot(second_id)
    assert snapshot.status == "completed"
    assert second_semantic.local_order == []
    assert snapshot.statistics["model_calls"] == 0
    assert snapshot.statistics["reused_local_nodes"] == 2
    assert store.get_current("shop", "orders", snapshot.api_key).id == second_id
    store.close()
