from fastapi.testclient import TestClient

from codeevolution.api import create_app
from codeevolution.domain.explanation import ExplanationNode, ExplanationSnapshot
from codeevolution.infrastructure.explanation_snapshot_store import ExplanationSnapshotStore


class SnapshotRuntimeStub:
    def __init__(self):
        self.store = self

    def get_snapshot(self, snapshot_id):
        return object() if snapshot_id == "repo-snapshot" else None

    def start(self):
        pass

    def close(self):
        pass


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


class NodeRuleQueryStub:
    def node_rule_context(self, snapshot_id, node_id):
        if snapshot_id != "repo-snapshot" or node_id != "orders::create":
            return None
        return {
            "repository_snapshot_id": snapshot_id,
            "member_id": "orders",
            "service": "orders",
            "member": "orders",
            "node_type": "func",
            "node_key": f"{snapshot_id}::{node_id}",
            "node_id": node_id,
            "qualified_name": "orders.create",
            "name": "create",
            "file": "orders.py",
            "line": 10,
            "end_line": 12,
            "snippet": "def create():\n    validate_order()\n    return save_order()",
        }


class NodeRuleRuntimeStub:
    class Store:
        def get_current_rule(self, **_kwargs):
            return None

    store = Store()

    def start(self):
        pass

    def close(self):
        pass


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


def _published_snapshot(store):
    snapshot = ExplanationSnapshot(
        id="current-snapshot", repo_name="shop", member_name="orders",
        api_key="POST|/orders|create_order", method="POST", path="/orders",
        handler="create_order", entry_node_key="orders::create_order",
        source_revision="rev", source_digest="source", graph_digest="graph",
        model_id="test-model", prompt_version="v1", schema_version="v1",
    )
    store.create_snapshot(snapshot)
    store.update_snapshot(snapshot.id, "completed", explanation={"summary": "创建订单"})
    store.publish(snapshot.id)
    return snapshot


class SourceLoaderStub:
    def __init__(self, source_digest):
        self.source_digest = source_digest

    def load(self, _spec):
        return {"source_digest": self.source_digest, "graph_digest": "graph"}


def test_current_explanation_reports_current_or_outdated_freshness(tmp_path):
    for expected, digest in (("current", "source"), ("outdated", "changed")):
        store = ExplanationSnapshotStore(tmp_path / f"{expected}.db")
        _published_snapshot(store)
        app = create_app({
            "explanation_snapshot_store": store,
            "explanation_source_loader": SourceLoaderStub(digest),
        })
        with TestClient(app) as client:
            response = client.get("/api/api-explanations/current", params={
                "repo": "shop", "member": "orders",
                "api_key": "POST|/orders|create_order",
            })
        assert response.status_code == 200
        assert response.json()["freshness"] == expected
        store.close()


def test_generate_accepts_repository_snapshot_without_repo(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    store = ExplanationSnapshotStore(tmp_path / "api.db")
    service = GenerationStub(store)

    class Queries:
        def open(self, snapshot_id):
            assert snapshot_id == "repo-snapshot"
            return nullcontext(SimpleNamespace(snapshot=SimpleNamespace(member_id="orders")))

    class Runtime:
        store = SimpleNamespace(add_snapshot_reference=lambda *_args, **_kwargs: None)

        def start(self):
            pass

        def close(self):
            pass

    app = create_app({
        "explanation_snapshot_store": store,
        "explanation_generation_service_factory": lambda: service,
        "snapshot_query_service": Queries(),
        "snapshot_runtime": Runtime(),
        "background_submit": lambda fn, *args: fn(*args),
    })
    with TestClient(app) as client:
        response = client.post("/api/api-explanations/generate", json={
            "repository_snapshot_id": "repo-snapshot", "method": "POST",
            "path": "/orders", "handler": "create_order",
        })

    assert response.status_code == 202
    assert service.specs[0]["repo"] == "orders"
    assert service.specs[0]["member"] == "orders"


def test_export_returns_one_document_per_endpoint(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")

    class Queries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "repo-snapshot"
            return {"endpoints": [{
                "method": "GET", "path": "/orders", "handler": "orders.list",
                "call_chain": [], "request_headers": [], "path_params": [],
                "query_params": [], "request_body": None, "response_body": None,
            }]}

    app = create_app({
        "explanation_snapshot_store": store,
        "snapshot_query_service": Queries(),
    })
    with TestClient(app) as client:
        response = client.get(
            "/api/api-explanations/export",
            params={"repository_snapshot_id": "repo-snapshot", "format": "markdown"},
        )

    assert response.status_code == 200
    documents = response.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["filename"].endswith(".md")
    assert documents[0]["content"].startswith("# GET /orders")
    assert "## Main Node Explanation" in documents[0]["content"]


def test_export_renders_business_description_and_rules_from_explanation(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")
    store.create_snapshot(ExplanationSnapshot(
        id="explained", repo_name="orders", member_name="orders",
        api_key="GET|/orders|orders.list", method="GET", path="/orders",
        handler="orders.list", entry_node_key="orders::list",
        source_revision="rev", source_digest="source", graph_digest="graph",
        model_id="test-model", prompt_version="v1", schema_version="v1",
        repository_snapshot_id="repo-snapshot",
    ))
    store.save_node(ExplanationNode(
        "explained", "orders::list", "hash", "digest", "agg",
        {"summary": "列出订单"},
        {
            "summary": "列出当前用户的订单",
            "business_rules": [{"rule": "仅返回已支付订单", "evidence_lines": [12]}],
            "side_effects": [],
        },
        "completed",
    ))
    store.update_snapshot("explained", "completed", explanation={"summary": "列出当前用户的订单"})
    store.publish("explained")

    class Queries:
        def api_contract(self, snapshot_id):
            assert snapshot_id == "repo-snapshot"
            return {"endpoints": [{
                "method": "GET", "path": "/orders", "handler": "orders.list",
                "call_chain": [], "request_headers": [], "path_params": [],
                "query_params": [], "request_body": None, "response_body": None,
            }]}

    app = create_app({
        "explanation_snapshot_store": store,
        "snapshot_query_service": Queries(),
    })
    with TestClient(app) as client:
        response = client.get(
            "/api/api-explanations/export",
            params={"repository_snapshot_id": "repo-snapshot", "format": "markdown"},
        )

    content = response.json()["documents"][0]["content"]
    assert "## Business Description" in content
    assert "列出当前用户的订单" in content
    assert "## Business Rules" in content
    assert "仅返回已支付订单 (source lines 12)" in content
    store.close()


def test_call_tree_node_rule_returns_frozen_source_context():
    app = create_app({
        "snapshot_query_service": NodeRuleQueryStub(),
        "snapshot_runtime": NodeRuleRuntimeStub(),
    })
    with TestClient(app) as client:
        response = client.get(
            "/api/call-tree/rule",
            params={"snapshot_id": "repo-snapshot", "node_id": "orders::create"},
        )

    assert response.status_code == 200
    assert response.json()["source"] == {
        "file": "orders.py",
        "start_line": 10,
        "end_line": 12,
        "content": "def create():\n    validate_order()\n    return save_order()",
    }


def test_prompt_api_returns_default_guidance_and_templates(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")
    runtime = SnapshotRuntimeStub()
    app = create_app({"snapshot_runtime": runtime, "explanation_snapshot_store": store})
    with TestClient(app) as client:
        response = client.get("/api/api-explanation-prompts", params={"repository_snapshot_id": "repo-snapshot"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_guidance"]
    assert set(payload["defaults"]) == {"local", "synthesis", "aggregate"}
    assert all(payload["defaults"].values())
    assert payload["current"] is None
    store.close()


def test_prompt_api_normalizes_empty_template_profile_to_defaults(tmp_path):
    store = ExplanationSnapshotStore(tmp_path / "api.db")
    runtime = SnapshotRuntimeStub()
    app = create_app({"snapshot_runtime": runtime, "explanation_snapshot_store": store})
    with TestClient(app) as client:
        defaults = client.get(
            "/api/api-explanation-prompts",
            params={"repository_snapshot_id": "repo-snapshot"},
        ).json()["defaults"]
        response = client.post(
            "/api/api-explanation-prompts",
            json={
                "repository_snapshot_id": "repo-snapshot",
                "prompt_text": "",
                "templates": {"local": "", "synthesis": "", "aggregate": ""},
            },
        )
        assert response.status_code == 201
        profile = response.json()["profile"]
        assert profile["prompt_templates"] == defaults

        current = client.get(
            "/api/api-explanation-prompts",
            params={"repository_snapshot_id": "repo-snapshot"},
        ).json()["current"]
        assert current["prompt_templates"] == defaults
    store.close()
