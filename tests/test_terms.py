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

    assert report["schema_version"] == "terms/v2"
    scores = [item["confidence_score"] for item in report["terms"]]
    assert scores == sorted(scores, reverse=True)
    terms = {(item["term_type"], item["canonical_name"]): item for item in report["terms"]}
    # the API resource and the domain entity dedupe into one entry
    assert terms[("ENTITY","Order")]["status"] == "accepted"
    assert ("API_RESOURCE","Order") not in terms
    evidence_types = {item["evidence_type"] for item in terms[("ENTITY","Order")]["evidence"]}
    assert {"api_route", "entity_definition"} <= evidence_types
    assert terms[("ENTITY","Order")]["source"].startswith("rule:")
    assert ("BUSINESS_ACTION","CreateOrder") not in terms
    # a single endpoint is one evidence source: the action needs human review
    assert terms[("BUSINESS_ACTION","ApproveOrder")]["status"] == "needs_review"
    relations = {(item["relationship"]): item for item in report["relations"]}
    assert "exposes" not in relations
    assert relations["applies_to"]["source_term_id"] == terms[("BUSINESS_ACTION","ApproveOrder")]["id"]


def test_same_repository_extracts_terms_for_multiple_snapshots(tmp_path):
    """Evidence ids must stay unique across snapshots of the same repository."""
    store = TermStore(tmp_path / "terms.db")
    service = TermRecognitionService(store)

    first = service.extract("snapshot-1", "orders", FACTS)
    second = service.extract("snapshot-2", "orders", FACTS)

    assert first["counts"]["total"] == second["counts"]["total"] >= 1
    first_evidence_ids = {item["id"] for term in first["terms"] for item in term["evidence"]}
    second_evidence_ids = {item["id"] for term in second["terms"] for item in term["evidence"]}
    assert first_evidence_ids and second_evidence_ids
    assert first_evidence_ids.isdisjoint(second_evidence_ids)
    term = next(item for item in second["terms"] if item["canonical_name"] == "Order")
    assert store.list_evidence("snapshot-2", term["id"])
    store.close()


def test_human_overrides_survive_reextraction(tmp_path):
    store = TermStore(tmp_path / "terms.db")
    service = TermRecognitionService(store)

    first = service.extract("snapshot-1", "orders", FACTS)
    order = next(item for item in first["terms"] if item["canonical_name"] == "Order")
    store.review("snapshot-1", order["id"], "reject", {}, "not a business term", "tester")
    store.review("snapshot-1", order["id"], "rename", {"canonical_name": "Purchase"}, "", "tester")
    store.add_manual("snapshot-1", "orders", {"canonical_name": "Invoice", "term_type": "ENTITY", "definition": "A bill"})

    second = service.extract("snapshot-1", "orders", FACTS)

    updated = next(item for item in second["terms"] if item["id"] == order["id"])
    assert updated["status"] == "rejected"
    assert updated["canonical_name"] == "Purchase"
    invoice = next(item for item in second["terms"] if item["canonical_name"] == "Invoice")
    assert invoice["status"] == "accepted"
    assert invoice["source"] == "manual"
    assert store.list_terms("snapshot-1", name="Purchase")["total"] == 1
    store.close()


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
    assert ("BUSINESS_ACTION","GetUserList") not in names
    assert ("BUSINESS_ACTION","CreateUser") not in names
    assert ("ENTITY","CreateUser") not in names
    assert ("API_RESOURCE","GetUserList") not in names
    # the /api/users resource dedupes into the User entity entry
    assert ("API_RESOURCE","User") not in names
    assert ("ENTITY","User") in names
    user = next(item for item in report["terms"] if item["term_type"] == "ENTITY" and item["canonical_name"] == "User")
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
    assert ("BUSINESS_ACTION","ViewReport") not in names
    assert ("BUSINESS_ACTION","CancelAnalysisRun") not in names
    assert ("BUSINESS_ACTION","StopUiRecording") not in names
    assert ("BUSINESS_ACTION","RunUiRecording") not in names
    assert ("API_RESOURCE","ViewReport") not in names
    assert ("API_RESOURCE","CancelAnalysisRun") not in names
    # noun+verb compounds are operations too
    assert ("BUSINESS_ACTION","TaskRun") not in names
    assert ("ENTITY","GraphView") not in names
    assert ("ENTITY","AnalysisRun") not in names
    assert ("API_RESOURCE","TaskRun") not in names
    # domain nouns and non-operation domain verbs survive untouched
    assert ("ENTITY","Report") in names
    assert ("BUSINESS_ACTION","PublishReport") in names
    # -sis nouns stay intact through singularization
    assert ("ENTITY","TaskAnalysisStatus") in names


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
    assert ("ENTITY","File") not in names
    assert ("API_RESOURCE","File") not in names
    assert ("BUSINESS_ACTION","ListFile") not in names
    # operation verbs keep being rejected
    assert ("BUSINESS_ACTION","EnableOrder") not in names
    # one deduplicated entry carries the merged evidence chain of both anchors
    orders = [item for item in report["terms"] if item["canonical_name"] == "Order"]
    assert len(orders) == 1
    order = orders[0]
    assert order["term_type"] == "ENTITY"
    kinds = {item["evidence_type"] for item in order["evidence"]}
    assert {"api_route", "entity_definition"} <= kinds
    assert all(item.get("node_id") or item.get("source_location") for item in order["evidence"])
    assert order["source"] == "rule:" + "+".join(sorted(kinds))


def test_frontend_pages_become_page_terms_with_titles():
    from codeevolution.analysis.knowledge.frontend_pages import FrontendPageExtractor

    class _Source:
        def list_files(self, categories=None, globs=None):
            return [
                {"path": "web/src/router/index.js"},
                {"path": "web/src/views/order/OrderList.vue"},
                {"path": "web/src/components/Table.vue"},
            ]

        def read_text(self, path):
            if path.endswith("router/index.js"):
                return (
                    "const routes = [{ path: '/orders', name: 'OrderManagement',"
                    " component: () => import('@/views/order/OrderList.vue'),"
                    " meta: { title: '订单管理' } },"
                    " { path: '/layout', name: 'MainLayout' }]"
                )
            return "<template><div/></template>"

    pages = FrontendPageExtractor(_Source()).extract()
    names = {page["name"]: page for page in pages}
    assert names["OrderManagement"]["title"] == "订单管理"
    assert names["OrderManagement"]["route"] == "/orders"
    assert names["OrderList"]["file"] == "web/src/views/order/OrderList.vue"
    # layout chrome never becomes a page
    assert "MainLayout" not in names
    assert "Table" not in names

    facts = {"frontend_pages": {"page_count": len(pages), "pages": pages}}
    report = TermRecognizer().extract("snapshot-1", "web", facts)
    page_terms = {item["canonical_name"]: item for item in report["terms"] if item["term_type"] == "PAGE"}
    assert "OrderManagement" in page_terms
    assert "订单管理" in page_terms["OrderManagement"]["aliases"]
    kinds = {item["evidence_type"] for item in page_terms["OrderManagement"]["evidence"]}
    assert {"frontend_page", "page_title"} <= kinds
    assert page_terms["OrderManagement"]["source"] == "rule:frontend_page+page_title"


def test_technical_wrapper_does_not_hide_a_real_entity():
    facts = {"api_contract": {"endpoints": []}, "core_entities": [
        {"node_id": "dto", "name": "OrderDTO", "file_path": "src/api.py", "kind": "class", "score": 20, "fields": []},
        {"node_id": "entity", "name": "Order", "file_path": "src/domain.py", "kind": "class", "score": 20, "fields": [{"name": "id"}]},
    ]}
    report = TermRecognizer().extract("snapshot-1", "orders", facts, ["ENTITY"])
    order = next(item for item in report["terms"] if item["canonical_name"] == "Order")
    # the DTO is an alias of the entity, not a second ENTITY candidate
    assert order["status"] in {"accepted", "needs_review"}
    assert "OrderDTO" in order["aliases"]


STRUCTURED_FACTS = {
    "api_contract": {"endpoints": [{
        "method": "POST", "path": "/api/orders", "handler": "orders::create_order",
        "node_id": "fn-create", "file": "orders/api.py", "line": 10,
        "request_body": {"type": "OrderRequest"},
    }]},
    "core_entities": [{
        "node_id": "type-order", "name": "Order", "qualified_name": "domain::Order",
        "file_path": "orders/domain.py", "kind": "class", "score": 24,
        "field_count": 2, "relationship_count": 2, "start_line": 4,
        "annotations": ["Entity"],
        "fields": [{"name": "id", "start_line": 5}, {"name": "totalAmount", "start_line": 6}],
    }],
    "module_topology": {"modules": [{
        "id": "orders", "name": "orders", "path": "orders", "primary_language": "python",
        "file_count": 4, "files": ["orders/api.py", "orders/domain.py", "orders/service.py"],
    }]},
    "term_facts": {
        "enums": [{
            "node_id": "enum-1", "name": "OrderStatus", "qualified_name": "domain::OrderStatus",
            "file_path": "orders/domain.py", "start_line": 20,
            "members": [{"name": "NEW"}, {"name": "PAID"}],
        }],
        "types": [
            {"node_id": "ev-1", "name": "OrderCreatedEvent", "base_name": "OrderCreated",
             "classification": "event", "kind": "class",
             "qualified_name": "events::OrderCreatedEvent", "file_path": "orders/events.py", "start_line": 5},
            {"node_id": "cmd-1", "name": "CreateOrderCommand", "base_name": "CreateOrder",
             "classification": "command", "kind": "class",
             "qualified_name": "commands::CreateOrderCommand", "file_path": "orders/commands.py", "start_line": 5},
            {"node_id": "dto-1", "name": "OrderResponse", "base_name": "Order",
             "classification": "dto", "kind": "class",
             "qualified_name": "api::OrderResponse", "file_path": "orders/api.py", "start_line": 30},
        ],
        "db_tables": [{
            "name": "orders", "file_path": "schema.sql", "start_line": 1,
            "columns": ["id", "total_amount", "payment_status"],
            "primary_key": ["id"], "source": "sql",
        }],
        "messages": [{
            "topic": "order.created", "symbol": "handle_order_created", "role": "consumer",
            "node_id": "fn-msg", "file_path": "orders/consumer.py", "start_line": 8,
        }],
        "service_methods": [{
            "name": "approveOrder", "qualified_name": "service::OrderService.approveOrder",
            "file_path": "orders/service.py", "start_line": 12,
        }],
        "languages": [{"language": "python", "file_count": 5}],
    },
}


def test_structured_facts_create_traceable_priority_candidates():
    report = TermRecognizer().extract("snapshot-1", "orders", STRUCTURED_FACTS)
    terms = {(item["term_type"], item["canonical_name"]): item for item in report["terms"]}

    order = terms[("ENTITY", "Order")]
    assert {"api_route", "entity_definition", "db_table", "dto_definition"} <= {
        item["evidence_type"] for item in order["evidence"]
    }
    assert order["priority"] == "P0"
    assert order["module"] == "orders" and order["language"] == "python"
    assert order["source_refs"]

    assert ("BUSINESS_ENUM", "OrderStatus") in terms
    assert ("COMMAND", "OrderCommand") in terms
    assert ("BUSINESS_EVENT", "OrderCreated") in terms
    assert ("BUSINESS_ATTRIBUTE", "TotalAmount") in terms
    assert ("BUSINESS_ATTRIBUTE", "PaymentStatus") in terms
    action = terms[("BUSINESS_ACTION", "ApproveOrder")]
    assert {"service_method"} <= {item["evidence_type"] for item in action["evidence"]}
    assert action["priority"] == "P1"

    relations = {(item["relationship"]) for item in report["relations"]}
    assert "has_attribute" in relations
    assert report["traceability"]["rate"] == 1.0
    assert report["counts"]["priorities"]["P0"] >= 1
    assets = report["asset_inventory"]
    assert assets["counts"]["db_table"] == 1
    assert assets["counts"]["message_topic"] == 1
    assert assets["counts"]["api_endpoint"] == 1


def test_conflicts_are_isolated_and_evidence_package_is_persisted(tmp_path):
    facts = dict(STRUCTURED_FACTS)
    facts["term_facts"] = {
        **STRUCTURED_FACTS["term_facts"],
        "types": [{
            "node_id": "ev-conflict", "name": "OrderEvent", "base_name": "Order",
            "classification": "event", "kind": "class",
            "qualified_name": "events::OrderEvent", "file_path": "orders/events.py", "start_line": 9,
        }],
    }
    store = TermStore(tmp_path / "terms.db")
    service = TermRecognitionService(store)
    report = service.extract("snapshot-1", "orders", facts)

    order = next(item for item in report["terms"] if item["canonical_name"] == "Order")
    assert order["status"] == "needs_review"
    assert "type_conflict" in order["risk_flags"]
    conflicts = store.list_conflicts("snapshot-1")
    assert conflicts and conflicts[0]["conflict_type"] == "same_name_multiple_types"

    package = store.get_evidence_package("snapshot-1", order["id"])
    assert package["term"]["id"] == order["id"]
    assert package["support"]
    assert "Contract" in package["independence_groups"]
    assert package["conflicts"]
    assert store.list_assets("snapshot-1", "db_table")[0]["name"] == "orders"
    store.close()


def test_alignment_recommends_connected_business_services_only():
    service = TermRecognitionService(None)
    reports = [
        {"repository_id": "orders", "service_id": "orders", "terms": [{"id": "order-a", "canonical_name": "Order", "term_type": "ENTITY", "status": "accepted"}]},
        {"repository_id": "billing", "service_id": "billing", "terms": [{"id": "order-b", "canonical_name": "Order", "term_type": "ENTITY", "status": "accepted"}]},
        {"repository_id": "gateway", "service_id": "gateway", "terms": [{"id": "order-c", "canonical_name": "Order", "term_type": "ENTITY", "status": "accepted"}]},
        {"repository_id": "inventory", "service_id": "inventory", "terms": [{"id": "sku", "canonical_name": "SKU", "term_type": "ENTITY", "status": "accepted"}]},
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


class _StructuredSnapshotQuery:
    def knowledge(self, snapshot_id):
        return STRUCTURED_FACTS


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


def test_terms_api_exposes_assets_conflicts_and_evidence_package(tmp_path):
    term_store = TermStore(tmp_path / "terms.db")
    snapshot_store = SimpleNamespace(get_snapshot=lambda snapshot_id: SimpleNamespace(member_id="orders"))
    app = create_app({
        "term_store": term_store,
        "snapshot_query_service": _StructuredSnapshotQuery(),
        "snapshot_runtime": _Runtime(snapshot_store),
    })
    with TestClient(app) as client:
        assert client.post("/api/terms/extract", json={"snapshot_id": "structured-1"}).status_code == 200
        assets = client.get("/api/terms/assets", params={"snapshot_id": "structured-1", "asset_type": "db_table"})
        assert assets.status_code == 200
        assert assets.json()["assets"][0]["name"] == "orders"

        conflicts = client.get("/api/terms/conflicts", params={"snapshot_id": "structured-1"})
        assert conflicts.status_code == 200

        term = client.get("/api/terms", params={"snapshot_id": "structured-1"}).json()["terms"][0]
        package = client.get(f"/api/terms/{term['id']}/evidence-package", params={"snapshot_id": "structured-1"})
        assert package.status_code == 200
        assert package.json()["package"]["support"]

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
