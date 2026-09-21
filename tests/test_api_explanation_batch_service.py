from types import SimpleNamespace

from fastapi.testclient import TestClient

from codeevolution.application.api_explanation_batch_service import ApiExplanationBatchService
from codeevolution.api import create_app
from codeevolution.infrastructure.explanation_snapshot_store import ExplanationSnapshotStore


class _BatchStore:
    def __init__(self):
        self.items = None

    def get_current_prompt_profile(self, _snapshot_id):
        return None

    def get_active_batch(self, _snapshot_id, _prompt_digest):
        return None

    def list_snapshots_for_repository_snapshot(self, _snapshot_id, _endpoint_key):
        return []

    def create_batch(self, _snapshot_id, _profile_id, _prompt_digest, items, *, concurrency):
        self.items = items
        return {"id": "batch-1", "items": items, "concurrency": concurrency}


def test_create_uses_complete_api_contract_for_every_endpoint():
    endpoints = [
        {"method": "GET", "path": f"/items/{index}", "handler": f"items.get_{index}"}
        for index in range(101)
    ]
    store = _BatchStore()

    class Queries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "snapshot-1"
            return {"endpoint_count": len(endpoints), "endpoints": endpoints}

        def knowledge(self, _snapshot_id):
            raise AssertionError("batch creation must not use the legacy knowledge payload")

    service = ApiExplanationBatchService(store, Queries(), lambda: SimpleNamespace())

    result = service.create("snapshot-1", concurrency=3)

    assert result["id"] == "batch-1"
    assert len(store.items) == 101
    assert store.items[-1]["path"] == "/items/100"


def test_batch_api_enqueues_every_endpoint_from_complete_contract():
    endpoints = [
        {"method": "GET", "path": f"/items/{index}", "handler": f"items.get_{index}"}
        for index in range(101)
    ]
    store = _BatchStore()

    class Queries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "snapshot-1"
            return {"endpoint_count": len(endpoints), "endpoints": endpoints}

        def knowledge(self, _snapshot_id):
            raise AssertionError("batch API must not use the legacy knowledge payload")

    service = ApiExplanationBatchService(store, Queries(), lambda: SimpleNamespace())

    class Scheduler:
        def __init__(self):
            self.service = service
            self.submitted = []

        def submit(self, batch_id):
            self.submitted.append(batch_id)

    scheduler = Scheduler()
    with TestClient(create_app({"api_explanation_batch_scheduler": scheduler})) as client:
        response = client.post(
            "/api/api-explanations/batches",
            json={"repository_snapshot_id": "snapshot-1", "concurrency": 3},
        )

    assert response.status_code == 202
    assert response.json()["batch"]["id"] == "batch-1"
    assert len(store.items) == 101
    assert scheduler.submitted == ["batch-1"]


def test_reused_explanations_count_as_a_completed_batch(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")
    item = {
        "endpoint_key": "GET|/items|items.list",
        "method": "GET",
        "path": "/items",
        "handler": "items.list",
        "status": "skipped",
        "skip_reason": "已存在相同提示词的完成结果",
        "explanation_snapshot_id": "snapshot-explanation",
    }

    batch = store.create_batch("snapshot-1", "prompt-1", "digest-1", [item])

    assert batch["status"] == "completed"
    assert batch["progress"]["skipped"] == 1
    store.close()
