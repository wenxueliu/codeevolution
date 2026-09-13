from types import SimpleNamespace

from fastapi.testclient import TestClient

from codeevolution.analysis.knowledge.terms import TermRecognizer
from codeevolution.api import create_app
from codeevolution.infrastructure.term_store import TermStore

FACTS = {
    "api_contract": {
        "endpoints": [{
            "method": "POST", "path": "/api/orders", "handler": "orders::create_order",
            "node_id": "fn-create", "file": "orders/api.py", "line": 10,
            "request_body": {"type": "OrderRequest", "model": {"name": "Order"}},
            "response_body": {"type": "OrderResponse", "model": {"name": "Order"}},
        }],
    },
    "core_entities": [{
        "node_id": "type-order", "name": "Order", "qualified_name": "domain::Order",
        "file_path": "src/domain/Order.py", "kind": "class", "score": 24,
        "field_count": 4, "relationship_count": 2, "start_line": 4,
        "annotations": ["Entity"], "fields": [{"name": "id"}],
    }],
}


def test_recognizer_prefers_api_and_entity_anchors_and_ranks_terms():
    report = TermRecognizer().extract("snapshot-1", "orders", FACTS)

    assert report["schema_version"] == "terms/v1"
    assert report["default_terms"][0]["confidence_score"] >= report["default_terms"][1]["confidence_score"]
    terms = {(item["term_type"], item["canonical_name"]): item for item in report["terms"]}
    assert terms[("entity", "Order")]["status"] == "accepted"
    assert terms[("resource", "Order")]["status"] == "accepted"
    assert terms[("resource", "Order")]["evidence_ids"]
    assert terms[("action", "CreateOrder")]["status"] == "accepted"
    assert any(item["relationship"] == "exposes" for item in report["relations"])


def test_technical_wrapper_does_not_hide_a_real_entity():
    facts = {"api_contract": {"endpoints": []}, "core_entities": [
        {"node_id": "dto", "name": "OrderDTO", "file_path": "src/api.py", "kind": "class", "score": 20, "fields": []},
        {"node_id": "entity", "name": "Order", "file_path": "src/domain.py", "kind": "class", "score": 20, "fields": [{"name": "id"}]},
    ]}
    report = TermRecognizer().extract("snapshot-1", "orders", facts, ["entity"])
    order = next(item for item in report["terms"] if item["canonical_name"] == "Order")
    assert order["status"] == "accepted"


class _Runtime:
    def __init__(self, store):
        self.store = store

    def start(self):
        pass

    def close(self):
        pass


class _SnapshotQuery:
    def knowledge(self, snapshot_id):
        if snapshot_id not in {"snapshot-1", "snapshot-2"}:
            raise KeyError(snapshot_id)
        return FACTS


def test_terms_api_extracts_lists_reviews_and_adds_manual_terms(tmp_path):
    term_store = TermStore(tmp_path / "terms.db")
    snapshot_store = SimpleNamespace(get_snapshot=lambda snapshot_id: SimpleNamespace(member_id="orders") if snapshot_id == "snapshot-1" else None)
    app = create_app({
        "term_store": term_store,
        "snapshot_query_service": _SnapshotQuery(),
        "snapshot_runtime": _Runtime(snapshot_store),
    })
    with TestClient(app) as client:
        extracted = client.post("/api/terms/extract", json={"snapshot_id": "snapshot-1"})
        assert extracted.status_code == 200
        assert extracted.json()["counts"]["accepted"] >= 1

        listing = client.get("/api/terms", params={"snapshot_id": "snapshot-1"})
        assert listing.status_code == 200
        term = next(item for item in listing.json()["terms"] if item["canonical_name"] == "Order")
        evidence = client.get(f"/api/terms/{term['id']}/evidence", params={"snapshot_id": "snapshot-1"})
        assert evidence.status_code == 200
        assert evidence.json()["evidence"]

        reviewed = client.post(f"/api/terms/{term['id']}/review", json={
            "snapshot_id": "snapshot-1", "action": "rename",
            "value": {"canonical_name": "PurchaseOrder"}, "author": "tester",
        })
        assert reviewed.status_code == 200
        assert reviewed.json()["term"]["canonical_name"] == "PurchaseOrder"

        manual = client.post("/api/terms/manual", json={
            "snapshot_id": "snapshot-1", "canonical_name": "SKU", "term_type": "value_object",
            "definition": "库存单位", "author": "tester",
        })
        assert manual.status_code == 201
        assert manual.json()["term"]["source"] == "manual"
        assert manual.json()["term"]["canonical_name"] == "SKU"
    term_store.close()


def test_terms_api_aligns_accepted_entities_from_a_view(tmp_path):
    term_store = TermStore(tmp_path / "terms.db")
    snapshot_store = SimpleNamespace(
        get_snapshot=lambda snapshot_id: SimpleNamespace(member_id="orders" if snapshot_id == "snapshot-1" else "billing") if snapshot_id in {"snapshot-1", "snapshot-2"} else None,
        get_view=lambda view_id: SimpleNamespace(members=[
            SimpleNamespace(member_id="orders", snapshot_id="snapshot-1"),
            SimpleNamespace(member_id="billing", snapshot_id="snapshot-2"),
        ]) if view_id == "view-1" else None,
    )
    app = create_app({
        "term_store": term_store,
        "snapshot_query_service": _SnapshotQuery(),
        "snapshot_runtime": _Runtime(snapshot_store),
    })
    with TestClient(app) as client:
        response = client.post("/api/terms/align", json={"view_id": "view-1"})
        assert response.status_code == 200
        assert response.json()["mappings"]
        assert response.json()["mappings"][0]["relationship"] == "same"
    term_store.close()
