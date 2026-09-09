import subprocess

from fastapi.testclient import TestClient

from codeevolution.api import create_app
from codeevolution.application.analysis_run_service import (
    AnalysisRunService,
    RepositoryCatalogService,
)
from codeevolution.application.repository_snapshot_service import (
    GraphViewService,
    RepositorySnapshotService,
)
from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore


def _client(tmp_path):
    store = AnalysisSnapshotSQLiteStore(tmp_path / "analysis.db")
    notifications = []
    application = create_app(
        {
            "catalog_service": RepositoryCatalogService(store),
            "analysis_run_service": AnalysisRunService(store),
            "repository_snapshot_service": RepositorySnapshotService(store),
            "graph_view_service": GraphViewService(store),
            "analysis_scheduler_notify": lambda: notifications.append(True),
        }
    )
    return TestClient(application), store, notifications


def test_catalog_run_cancel_and_current_view_api(tmp_path):
    client, _store, notifications = _client(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    with client:
        scope_response = client.post("/api/scopes", json={"name": "shop"})
        assert scope_response.status_code == 201
        scope_id = scope_response.json()["scope"]["id"]
        assert client.get("/api/scopes").json()["scopes"][0]["id"] == scope_id

        member_response = client.post(
            f"/api/scopes/{scope_id}/members",
            json={"display_name": "orders", "registered_path": str(repo)},
        )
        assert member_response.status_code == 201
        member_id = member_response.json()["member"]["id"]

        run_response = client.post("/api/analysis-runs", json={"member_ids": [member_id]})
        assert run_response.status_code == 202
        run_id = run_response.json()["run"]["id"]
        assert run_response.headers["location"] == f"/api/analysis-runs/{run_id}"
        assert notifications == [True]
        assert run_response.json()["run"]["members"][0]["attempt"]["status"] == "pending"

        cancelled = client.post(f"/api/analysis-runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["run"]["status"] == "cancelled"

        view_response = client.post(
            "/api/graph-views/current", json={"member_ids": [member_id]}
        )
        assert view_response.status_code == 201
        view = view_response.json()["view"]
        assert view["completeness"] == "incomplete"
        assert view["members"][0]["availability"] == "unparsed"
        assert client.get(f"/api/graph-views/{view['id']}").status_code == 200


def test_run_api_rejects_unknown_member_and_missing_resources(tmp_path):
    client, _store, _notifications = _client(tmp_path)
    with client:
        assert client.post("/api/analysis-runs", json={"member_ids": ["missing"]}).status_code == 422
        assert client.get("/api/analysis-runs/missing").status_code == 404
        assert client.get("/api/repository-snapshots/missing").status_code == 404


def test_scope_delete_retires_catalog_members_without_deleting_snapshot_data(tmp_path):
    client, _store, _notifications = _client(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    with client:
        scope = client.post("/api/scopes", json={"name": "shop"}).json()["scope"]
        member = client.post(
            f"/api/scopes/{scope['id']}/members",
            json={"display_name": "shop", "registered_path": str(repo)},
        ).json()["member"]

        deleted = client.delete(f"/api/scopes/{scope['id']}")

        assert deleted.status_code == 200
        assert deleted.json()["scope"]["id"] == scope["id"]
        assert client.get("/api/scopes").json()["scopes"] == []
        assert client.get(f"/api/scopes/{scope['id']}/members").status_code == 200
        assert client.get(f"/api/scopes/{scope['id']}/members").json()["members"] == []
        assert _store.get_member(member["id"]).retired_at is not None
