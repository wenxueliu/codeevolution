from types import SimpleNamespace

from codehistory.application.evolution_service import EvolutionQueryService
from codehistory.application.topology_service import TopologyService


class FakeCache:
    def __init__(self, value=None):
        self.value = value
        self.saved = []

    def load(self):
        return self.value

    def save(self, value):
        self.saved.append(value)


def test_topology_cache_hit_miss_force_and_single_build():
    calls = []
    builder = SimpleNamespace(build=lambda: calls.append("build") or {"fresh": True})
    cache = FakeCache({"cached": True})
    service = TopologyService(builder, cache)
    assert service.get_or_build() == {"cached": True}
    assert calls == []
    assert service.get_or_build(force=True) == {"fresh": True}
    assert calls == ["build"]
    assert cache.saved == [{"fresh": True}]


def test_impact_builds_topology_only_once():
    calls = []
    builder = SimpleNamespace(build=lambda: calls.append("build") or "topology")
    analyzer = SimpleNamespace(analyze=lambda topology, service: (topology, service))
    result = TopologyService(builder).impact(analyzer, "orders")
    assert result == ("topology", "orders")
    assert calls == ["build"]


def test_evolution_service_filters_and_pages():
    features = [
        {"canonical_name": "Orders", "entry_signature": "GET /orders", "status": "active"},
        {"canonical_name": "Users", "entry_signature": "GET /users", "status": "removed"},
    ]
    store = SimpleNamespace(get_all_features=lambda: features)
    result = EvolutionQueryService(store).list_features(status="active", search="order")
    assert result == {"total": 1, "features": [features[0]]}
