import json

from codeevolution.application.snapshot_runtime import SnapshotRuntime


class _Worker:
    def execute(self, attempt_id, cancellation):
        raise AssertionError("worker should not run during runtime construction")


def test_runtime_does_not_revalidate_mutated_legacy_registry_after_migration(tmp_path):
    data_root = tmp_path / "data"
    registry = tmp_path / "legacy" / "registry.json"
    registry.parent.mkdir()
    registry.write_text(json.dumps([{"name": "orders", "path": "/repos/orders"}]))

    first = SnapshotRuntime(data_root, legacy_registry=registry, worker=_Worker())
    first.close()
    registry.write_text("[]")

    restarted = SnapshotRuntime(data_root, legacy_registry=registry, worker=_Worker())
    try:
        assert [scope.name for scope in restarted.store.list_scopes()] == ["orders"]
    finally:
        restarted.close()
