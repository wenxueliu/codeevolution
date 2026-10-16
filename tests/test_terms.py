from types import SimpleNamespace

from fastapi.testclient import TestClient

from codeevolution.analysis.knowledge.terms import TermRecognizer
from codeevolution.api import create_app
from codeevolution.application.term_service import TermRecognitionService
from codeevolution.infrastructure.term_store import TermStore

FACTS = {
    "api_contract": {
        "endpoints": [
            {
                "method": "POST", "path": "/api/orders", "handler": "orders::create_order",
                "node_id": "fn-create", "file": "orders/api.py", "line": 10,
                "request_body": {"type": "OrderRequest", "model": {"name": "Order"}},
                "response_body": {"type": "OrderResponse", "model": {"name": "Order"}},
            },
            {
                "method": "POST", "path": "/api/orders/{id}/approval", "handler": "orders::approve_order",
                "node_id": "fn-approve", "file": "orders/api.py", "line": 20,
            },
        ],
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
    # the API resource and the domain entity dedupe into one entry
    assert terms[("entity", "Order")]["status"] == "accepted"
    assert ("resource", "Order") not in terms
    evidence_types = {item["evidence_type"] for item in terms[("entity", "Order")]["evidence"]}
    assert {"api_route", "entity_definition"} <= evidence_types
    assert terms[("entity", "Order")]["source"].startswith("rule:")
    assert ("action", "CreateOrder") not in terms
    assert terms[("action", "ApproveOrder")]["status"] == "accepted"
    relations = {(item["relationship"]): item for item in report["relations"]}
    assert "exposes" not in relations
    assert relations["applies_to"]["source_term_id"] == terms[("action", "ApproveOrder")]["id"]


def test_data_operation_names_do_not_become_terms():
    facts = {
        "api_contract": {"endpoints": [
            {"method": "GET", "path": "/api/get_user_list", "handler": "users::get_user_list", "node_id": "fn-1"},
            {"method": "POST", "path": "/api/users", "handler": "users::create_user", "node_id": "fn-2",
             "request_body": {"type": "CreateUserRequest"}},
        ]},
        "core_entities": [
            {"node_id": "t1", "name": "User", "file_path": "src/domain.py", "kind": "class", "score": 30,
             "fields": [{"name": "id"}]},
            {"node_id": "t2", "name": "CreateUserRequest", "file_path": "src/dto.py", "kind": "class", "score": 10,
             "fields": []},
        ],
    }
    report = TermRecognizer().extract("snapshot-1", "users", facts)

    names = {(item["term_type"], item["canonical_name"]) for item in report["terms"]}
    assert ("action", "GetUserList") not in names
    assert ("action", "CreateUser") not in names
    assert ("entity", "CreateUser") not in names
    assert ("resource", "GetUserList") not in names
    # the /api/users resource dedupes into the User entity entry
    assert ("resource", "User") not in names
    assert ("entity", "User") in names
    user = next(item for item in report["terms"] if item["term_type"] == "entity" and item["canonical_name"] == "User")
    assert "CreateUserRequest" in user["aliases"]


def test_operation_verb_compounds_do_not_become_terms():
    facts = {
        "api_contract": {"endpoints": [
            {"method": "POST", "path": "/api/view-report", "handler": "reports::view_report", "node_id": "fn-1"},
            {"method": "POST", "path": "/api/cancel-analysis-run", "handler": "runs::cancel_analysis_run", "node_id": "fn-2"},
            {"method": "POST", "path": "/api/stop-recording", "handler": "rec::stop_ui_recording", "node_id": "fn-3"},
            {"method": "POST", "path": "/api/run-recording", "handler": "rec::run_ui_recording", "node_id": "fn-4"},
            {"method": "POST", "path": "/api/task-run", "handler": "tasks::task_run", "node_id": "fn-5"},
            {"method": "POST", "path": "/api/reports", "handler": "reports::publish_report", "node_id": "fn-6"},
        ]},
        "core_entities": [
            {"node_id": "t1", "name": "GraphView", "file_path": "src/web.py", "kind": "class", "score": 30, "fields": []},
            {"node_id": "t2", "name": "AnalysisRun", "file_path": "src/run.py", "kind": "class", "score": 30, "fields": []},
            {"node_id": "t3", "name": "TaskAnalysisStatus", "file_path": "src/run.py", "kind": "class", "score": 20, "fields": []},
            {"node_id": "t4", "name": "Report", "file_path": "src/domain.py", "kind": "class", "score": 30, "fields": [{"name": "id"}]},
        ],
    }
    report = TermRecognizer().extract("snapshot-1", "ops", facts)

    names = {(item["term_type"], item["canonical_name"]) for item in report["terms"]}
    # verb-led compounds are operations on terms, not terms
    assert ("action", "ViewReport") not in names
    assert ("action", "CancelAnalysisRun") not in names
    assert ("action", "StopUiRecording") not in names
    assert ("action", "RunUiRecording") not in names
    assert ("resource", "ViewReport") not in names
    assert ("resource", "CancelAnalysisRun") not in names
    # noun+verb compounds are operations too
    assert ("action", "TaskRun") not in names
    assert ("entity", "GraphView") not in names
    assert ("entity", "AnalysisRun") not in names
    assert ("resource", "TaskRun") not in names
    # domain nouns and non-operation domain verbs survive untouched
    assert ("entity", "Report") in names
    assert ("action", "PublishReport") in names
    # -sis nouns stay intact through singularization
    assert ("entity", "TaskAnalysisStatus") in names


def test_terms_dedupe_across_types_and_drop_abstract_words():
    facts = {
        "api_contract": {"endpoints": [
            {"method": "GET", "path": "/api/files", "handler": "files::list_files", "node_id": "fn-1"},
            {"method": "POST", "path": "/api/orders", "handler": "orders::enable_order", "node_id": "fn-2"},
        ]},
        "core_entities": [
            {"node_id": "t1", "name": "File", "file_path": "src/fs.py", "kind": "class", "score": 30, "fields": []},
            {"node_id": "t2", "name": "Order", "file_path": "src/domain.py", "kind": "class", "score": 30,
             "fields": [{"name": "id"}]},
        ],
    }
    report = TermRecognizer().extract("snapshot-1", "dedup", facts)

    names = {(item["term_type"], item["canonical_name"]) for item in report["terms"]}
    # abstract structural words are not terms, even with API + entity anchors
    assert ("entity", "File") not in names
    assert ("resource", "File") not in names
    assert ("action", "ListFile") not in names
    # operation verbs keep being rejected
    assert ("action", "EnableOrder") not in names
    # one deduplicated entry carries the merged evidence chain of both anchors
    orders = [item for item in report["terms"] if item["canonical_name"] == "Order"]
    assert len(orders) == 1
    order = orders[0]
    assert order["term_type"] == "entity"
    kinds = {item["evidence_type"] for item in order["evidence"]}
    assert {"api_route", "entity_definition"} <= kinds
    assert all(item.get("node_id") or item.get("source_location") for item in order["evidence"])
    assert order["source"] == "rule:" + "+".join(sorted(kinds))


def test_technical_wrapper_does_not_hide_a_real_entity():
    facts = {"api_contract": {"endpoints": []}, "core_entities": [
        {"node_id": "dto", "name": "OrderDTO", "file_path": "src/api.py", "kind": "class", "score": 20, "fields": []},
        {"node_id": "entity", "name": "Order", "file_path": "src/domain.py", "kind": "class", "score": 20, "fields": [{"name": "id"}]},
    ]}
    report = TermRecognizer().extract("snapshot-1", "orders", facts, ["entity"])
    order = next(item for item in report["terms"] if item["canonical_name"] == "Order")
    assert order["status"] == "accepted"


def test_alignment_recommends_connected_business_services_only():
    service = TermRecognitionService(None)
    reports = [
        {"repository_id": "orders", "service_id": "orders", "terms": [{"id": "order-a", "canonical_name": "Order", "term_type": "entity", "status": "accepted"}]},
        {"repository_id": "billing", "service_id": "billing", "terms": [{"id": "order-b", "canonical_name": "Order", "term_type": "entity", "status": "accepted"}]},
        {"repository_id": "gateway", "service_id": "gateway", "terms": [{"id": "order-c", "canonical_name": "Order", "term_type": "entity", "status": "accepted"}]},
        {"repository_id": "inventory", "service_id": "inventory", "terms": [{"id": "sku", "canonical_name": "SKU", "term_type": "entity", "status": "accepted"}]},
    ]
    result = service.align(reports, service_edges=[
        {"source_member_id": "orders", "target_member_id": "billing", "kind": "http"},
        {"source_member_id": "gateway", "target_member_id": "orders", "kind": "http"},
    ])

    assert {(item["source_service_id"], item["target_service_id"]) for item in result["service_pairs"]} == {("billing", "orders")}
    assert result["excluded_services"] == [{"service_id": "gateway", "display_name": "gateway", "reason": "technical_service"}]
    assert len(result["mappings"]) == 1


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


class _SnapshotTopology:
    def topology(self, view_id):
        return {"view_id": view_id, "edges": [{
            "source_member_id": "orders", "target_member_id": "billing", "kind": "http",
        }]}


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

        rejected = client.post(f"/api/terms/{term['id']}/review", json={
            "snapshot_id": "snapshot-1", "action": "reject", "value": {}, "author": "tester",
        })
        assert rejected.status_code == 200
        assert rejected.json()["term"]["status"] == "rejected"

        restored = client.post(f"/api/terms/{term['id']}/review", json={
            "snapshot_id": "snapshot-1", "action": "accept", "value": {}, "author": "tester",
        })
        assert restored.status_code == 200
        assert restored.json()["term"]["status"] == "accepted"

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
        "snapshot_topology_service": _SnapshotTopology(),
        "snapshot_runtime": _Runtime(snapshot_store),
    })
    with TestClient(app) as client:
        response = client.post("/api/terms/align", json={"view_id": "view-1"})
        assert response.status_code == 200
        assert response.json()["mappings"]
        assert response.json()["mappings"][0]["relationship"] == "same"
        assert response.json()["mappings"][0]["status"] == "needs_review"
        assert response.json()["service_pairs"][0]["reasons"] == ["http"]

        alignments = client.get("/api/terms/alignments", params={"view_id": "view-1"})
        assert alignments.status_code == 200
        alignment_id = alignments.json()["alignments"][0]["id"]
        reviewed = client.post(
            f"/api/terms/alignments/{alignment_id}/review",
            params={"view_id": "view-1"},
            json={"action": "accept", "author": "tester"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["alignment"]["status"] == "accepted"
    term_store.close()
