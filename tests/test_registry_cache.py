import json

import pytest

from codehistory.infrastructure.registry_json import RegistryRepository
from codehistory.infrastructure.topology_cache_json import CACHE_SCHEMA_VERSION, TopologyCache


def test_registry_round_trip_and_corrupt_json(tmp_path):
    path = tmp_path / "registry.json"
    repository = RegistryRepository(path)
    repository.save([{"name": "orders"}])
    assert repository.load() == [{"name": "orders"}]
    path.write_text("broken", encoding="utf-8")
    assert repository.load() == []


def test_topology_cache_versions_new_data_and_reads_legacy(tmp_path):
    path = tmp_path / "topology.json"
    path.write_text(json.dumps({"services": []}), encoding="utf-8")
    cache = TopologyCache(path)
    assert cache.load() == {"services": [], "schema_version": 0}

    cache.save({"services": ["orders"]})
    assert cache.load() == {
        "services": ["orders"],
        "schema_version": CACHE_SCHEMA_VERSION,
    }


def test_failed_atomic_replace_preserves_previous_cache(tmp_path, monkeypatch):
    path = tmp_path / "topology.json"
    cache = TopologyCache(path)
    cache.save({"services": ["valid"]})

    def fail_replace(source, destination):
        raise OSError("disk failure")

    monkeypatch.setattr("codehistory.infrastructure.registry_json.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        cache.save({"services": ["invalid"]})
    assert json.loads(path.read_text(encoding="utf-8"))["services"] == ["valid"]
