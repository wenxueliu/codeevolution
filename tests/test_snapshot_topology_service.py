from types import SimpleNamespace

from codeevolution.application.snapshot_topology_service import SnapshotTopologyService


class _Store:
    def get_view(self, view_id):
        if view_id != "view-1":
            return None
        return SimpleNamespace(
            id=view_id,
            digest="digest",
            completeness=SimpleNamespace(value="complete"),
            members=[
                SimpleNamespace(member_id="mall", snapshot_id="snapshot-mall", availability=SimpleNamespace(value="available")),
                SimpleNamespace(member_id="mall-admin-web", snapshot_id="snapshot-web", availability=SimpleNamespace(value="available")),
            ],
        )


class _Queries:
    def knowledge(self, snapshot_id):
        if snapshot_id == "snapshot-mall":
            return {
                "api_contract": {"endpoints": []},
                "external_dependencies": {"by_category": [{"category": "database", "dependencies": []}]},
            }
        return {"api_contract": {"endpoints": []}, "external_dependencies": {"by_category": []}}


def test_impact_ignores_dependency_edges_without_a_target_member():
    result = SnapshotTopologyService(_Store(), _Queries()).impact("view-1", "mall")

    assert result["service"] == "mall"
    assert [item["member_id"] for item in result["affected"]] == ["mall"]
    assert result["edges"][0]["kind"] == "dependency"
