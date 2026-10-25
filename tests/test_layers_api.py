import json

from fastapi.testclient import TestClient

from codeevolution import api as api_module
from codeevolution.api import create_app


class _Graph:
    def layer_call_edges(self):
        return [
            {
                "source_file": "app/web/orders.py",
                "source_name": "create_order",
                "target_file": "app/domain/order.py",
                "target_name": "Order",
                "call_line": 12,
            }
        ]

    def get_all_files(self):
        return ["app/web/orders.py", "app/domain/order.py"]


class _Handle:
    graph = _Graph()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _declaration(allow=None):
    return {
        "version": "layers/v1",
        "layers": {"presentation": ["**/web/**"], "domain": ["**/domain/**"]},
        "rules": {"strict": True, "allow": allow or [], "ignore": []},
    }


def _client(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(api_module, "_layers_repo_root", lambda _snapshot_id: repository)
    monkeypatch.setattr(api_module, "_layers_graph", lambda _snapshot_id: _Handle())
    return TestClient(create_app({})), repository


def test_read_save_and_check_layers_for_a_snapshot(tmp_path, monkeypatch):
    client, repository = _client(tmp_path, monkeypatch)

    with client:
        initial = client.get("/api/repository-snapshots/snap-1/layers")
        assert initial.status_code == 200
        payload = initial.json()
        assert payload["exists"] is False
        assert payload["baseline_pairs"] == 0
        assert payload["config"]["origin"] == "builtin"
        assert payload["config_path"] == str(repository / ".codeevolution" / "layers.json")

        saved = client.put(
            "/api/repository-snapshots/snap-1/layers", json={"config": _declaration()}
        )
        assert saved.status_code == 200
        assert (repository / ".codeevolution" / "layers.json").is_file()

        reread = client.get("/api/repository-snapshots/snap-1/layers")
        assert reread.json()["exists"] is True
        assert reread.json()["config"]["layers"]["presentation"] == ["**/web/**"]

        checked = client.post(
            "/api/repository-snapshots/snap-1/layers/check", json={"config": _declaration()}
        )
        assert checked.status_code == 200
        result = checked.json()
        assert result["new_count"] == 1
        assert result["groups"][0]["from"] == "presentation"
        assert result["groups"][0]["to"] == "domain"


def test_check_uses_saved_declaration_when_no_draft_is_sent(tmp_path, monkeypatch):
    client, repository = _client(tmp_path, monkeypatch)
    config_path = repository / ".codeevolution" / "layers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(_declaration()), encoding="utf-8")

    with client:
        checked = client.post("/api/repository-snapshots/snap-1/layers/check")
        assert checked.status_code == 200
        assert checked.json()["new_count"] == 1

        baselined = client.post("/api/repository-snapshots/snap-1/layers/baseline")
        assert baselined.status_code == 200
        assert baselined.json()["pairs"] == 1
        assert (repository / ".codeevolution" / "layers.baseline.json").is_file()

        after = client.post(
            "/api/repository-snapshots/snap-1/layers/check", json={"config": _declaration()}
        )
        assert after.json()["new_count"] == 0
        assert after.json()["baselined"] == 1
