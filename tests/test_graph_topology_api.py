import json

from fastapi.testclient import TestClient

from codeevolution.api import create_app


class _Store:
    def get_artifact_job(self, job_id):
        return {"id": job_id, "status": "pending", "artifact_kind": "topology"}


class _ArtifactService:
    def __init__(self):
        self.store = _Store()
        self.created = []
        self.payload = {
            "schema_version": "topology-artifact/v1",
            "services": [
                {"member_id": "gateway", "entries": [{"entry_id": "root", "method": "GET", "path_template": "/"}]},
                {"member_id": "orders", "entries": [{"entry_id": "orders.root", "method": "GET", "path_template": "/orders"}]},
            ],
            "service_projections": [{"kind": "http", "source_member_id": "gateway", "target_member_id": "orders"}],
            "endpoint_dependencies": [{"kind": "http", "edge_id": "edge-1", "source": {"member_id": "gateway", "entry_id": "root"}, "target": {"member_id": "orders", "entry_ids": ["orders.root"]}}],
            "resource_dependencies": [], "coverage": {"status": "complete", "unknown_boundaries": []}, "candidates": [],
        }

    def create_job(self, view_id, artifact_kind, params):
        self.created.append((view_id, artifact_kind, params))
        return {"id": "job-1", "status": "pending", "artifact_kind": artifact_kind}

    def get(self, view_id, artifact_kind, params):
        return {"status": "completed", "payload_json": json.dumps(self.payload), "payload_digest": "sha256:payload"}


def test_new_graph_artifact_and_query_contracts_are_explicit_and_read_only():
    service = _ArtifactService()
    client = TestClient(create_app({"graph_artifact_service": service}))

    with client:
        created = client.post(
            "/api/graph-views/view-1/artifact-jobs",
            json={"artifact_kind": "topology", "params": {}},
        )
        assert created.status_code == 202
        assert created.headers["location"].endswith("/job-1")
        assert service.created == [("view-1", "topology", {})]

        artifact = client.get("/api/graph-views/view-1/artifacts/topology")
        assert artifact.status_code == 200
        assert "view_id" not in artifact.json()["artifact"]
        assert artifact.headers["etag"] == '"sha256:payload"'

        impact = client.get("/api/graph-views/view-1/impact", params={"member_id": "gateway"})
        assert impact.status_code == 200
        assert impact.json()["downstream_dependencies"][0]["member_id"] == "orders"

        flow = client.get("/api/graph-views/view-1/flow", params={"member_id": "gateway", "entry_id": "root"})
        assert flow.status_code == 200
        assert flow.json()["root"]["entry_id"] == "root"


def test_artifact_post_rejects_non_topology_or_filtered_variant():
    service = _ArtifactService()
    client = TestClient(create_app({"graph_artifact_service": service}))

    with client:
        invalid = client.post(
            "/api/graph-views/view-1/artifact-jobs",
            json={"artifact_kind": "topology", "params": {"channels": ["http"]}},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_artifact_request"
