from fastapi.testclient import TestClient

from codeevolution.api import create_app
from codeevolution.domain.explanation import ExplanationNode, ExplanationSnapshot
from codeevolution.infrastructure.explanation_snapshot_store import ExplanationSnapshotStore


class GenerationStub:
    def __init__(self, store):
        self.store = store
        self.specs = []

    def prepare(self, spec):
        self.specs.append(spec)
        snapshot = ExplanationSnapshot(
            id="candidate", repo_name=spec["repo"], member_name=spec["member"],
            api_key="POST|/orders|create_order", method="POST", path="/orders",
            handler="create_order", entry_node_key="orders::create_order",
            source_revision="abc", source_digest="source", graph_digest="graph",
            model_id="test-model", prompt_version="v1", schema_version="v1",
        )
        self.store.create_snapshot(snapshot)
        return snapshot.id, {}

    def generate(self, snapshot_id, frozen):
        self.store.update_snapshot(snapshot_id, "running")
        self.store.save_node(
            ExplanationNode(snapshot_id, "orders::create_order", "s", "l", "a",
                            {"summary": "本地"}, {"summary": "创建订单"}, "completed")
        )
        self.store.update_snapshot(snapshot_id, "completed", explanation={"summary": "创建订单"})
        self.store.publish(snapshot_id)


def test_manual_generation_current_listing_and_confirmed_delete(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")
    service = GenerationStub(store)
    app = create_app({
        "explanation_snapshot_store": store,
        "explanation_generation_service_factory": lambda: service,
        "background_submit": lambda fn, *args: fn(*args),
    })
    with TestClient(app) as client:
        response = client.post("/api/api-explanations/generate", json={
            "repo": "shop", "member": "orders", "method": "POST",
            "path": "/orders", "handler": "create_order", "file": "orders.py", "line": 1,
            "custom_prompt": "重点关注库存扣减",
        })
        assert response.status_code == 202
        assert response.json()["snapshot"]["status"] == "pending"

        current = client.get("/api/api-explanations/current", params={
            "repo": "shop", "member": "orders", "api_key": "POST|/orders|create_order",
        }).json()["snapshot"]
        assert current["explanation"]["summary"] == "创建订单"
        assert current["nodes"][0]["node_key"] == "orders::create_order"

        snapshots = client.get("/api/api-explanations/snapshots", params={
            "repo": "shop", "member": "orders", "api_key": "POST|/orders|create_order",
        }).json()["snapshots"]
        assert [item["id"] for item in snapshots] == ["candidate"]

        assert client.delete("/api/api-explanations/snapshots/candidate").status_code == 409
        assert client.delete(
            "/api/api-explanations/snapshots/candidate?confirm_current=true"
        ).status_code == 200
        missing = client.get("/api/api-explanations/current", params={
            "repo": "shop", "member": "orders", "api_key": "POST|/orders|create_order",
        }).json()
        assert missing == {"status": "missing", "snapshot": None}
        assert service.specs[0]["_prompt_text"] == "重点关注库存扣减"
        assert service.specs[0]["_prompt_version"].startswith("custom-")
    store.close()
