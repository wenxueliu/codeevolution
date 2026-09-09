import json

import pytest

from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore
from codeevolution.infrastructure.registry_snapshot_migration import (
    RegistrySnapshotMigrationConflictError,
    RegistrySnapshotMigrationError,
    legacy_registry_migrated,
    migrate_legacy_registry,
)


def _store(tmp_path):
    return AnalysisSnapshotSQLiteStore(tmp_path / "data" / "analysis-snapshots.db")


def test_migrates_legacy_and_grouped_registries_and_is_strictly_idempotent(tmp_path):
    source = tmp_path / "legacy" / "registry.json"
    source.parent.mkdir()
    orders = tmp_path / "repos" / "orders"
    backend = tmp_path / "repos" / "mall-api"
    frontend = tmp_path / "repos" / "mall-web"
    source.write_text(
        json.dumps(
            [
                {"name": "orders-service", "path": str(orders)},
                {
                    "name": "mall",
                    "path": str(backend),
                    "repositories": [
                        {"name": "backend", "path": str(backend)},
                        {"name": "frontend", "path": str(frontend)},
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )
    store = _store(tmp_path)

    first = migrate_legacy_registry(source, tmp_path / "data", store)
    scopes = store.list_scopes()
    members = store.list_members()
    ids = ([scope.id for scope in scopes], [member.id for member in members])
    second = migrate_legacy_registry(source, tmp_path / "data", store)

    assert first.status == "migrated"
    assert (first.scope_count, first.member_count) == (2, 3)
    assert first.backup_path.read_bytes() == source.read_bytes()
    assert first.backup_path.stat().st_mode & 0o777 == 0o600
    assert {scope.name for scope in scopes} == {"orders-service", "mall"}
    assert {member.display_name for member in members} == {
        "orders-service",
        "backend",
        "frontend",
    }
    assert second.status == "already_migrated"
    assert second.backup_path == first.backup_path
    assert ids == (
        [scope.id for scope in store.list_scopes()],
        [member.id for member in store.list_members()],
    )

    # Deterministic IDs do not depend on which catalog file receives the import.
    other_store = AnalysisSnapshotSQLiteStore(tmp_path / "other" / "analysis.db")
    migrate_legacy_registry(source, tmp_path / "other", other_store)
    assert sorted(scope.id for scope in scopes) == sorted(
        scope.id for scope in other_store.list_scopes()
    )
    assert sorted(member.id for member in members) == sorted(
        member.id for member in other_store.list_members()
    )


@pytest.mark.parametrize(
    "content",
    [b"not-json", b"{}", b'[{"name":"orders"}]', b'[{"name":3,"path":"/repo"}]'],
)
def test_malformed_registry_fails_without_creating_marker_or_catalog(tmp_path, content):
    source = tmp_path / "registry.json"
    source.write_bytes(content)
    store = _store(tmp_path)

    with pytest.raises(RegistrySnapshotMigrationError):
        migrate_legacy_registry(source, tmp_path / "data", store)

    assert store.list_scopes() == []
    with store.connection() as connection:
        marker_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_markers'"
        ).fetchone()
    assert marker_table is None


@pytest.mark.parametrize(
    "entries, message",
    [
        (
            [
                {"name": "one", "path": "/repos/shared"},
                {"name": "two", "path": "/repos/shared"},
            ],
            "duplicate repository path",
        ),
        (
            [
                {"name": "one", "path": "/first/api"},
                {"name": "two", "path": "/second/api"},
            ],
            "duplicate repository basename",
        ),
        (
            [
                {
                    "name": "group",
                    "path": "/repos/one",
                    "repositories": [
                        {"name": "same", "path": "/repos/one"},
                        {"name": "same", "path": "/repos/two"},
                    ],
                }
            ],
            "duplicate member name",
        ),
    ],
)
def test_rejects_ambiguous_registry(entries, message, tmp_path):
    source = tmp_path / "registry.json"
    source.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(RegistrySnapshotMigrationError, match=message):
        migrate_legacy_registry(source, tmp_path / "data", _store(tmp_path))


def test_changed_source_after_migration_is_rejected_without_partial_import(tmp_path):
    source = tmp_path / "registry.json"
    source.write_text(json.dumps([{"name": "one", "path": "/repos/one"}]))
    store = _store(tmp_path)
    migrate_legacy_registry(source, tmp_path / "data", store)
    original_member_ids = [member.id for member in store.list_members()]
    source.write_text(json.dumps([{"name": "two", "path": "/repos/two"}]))

    with pytest.raises(RegistrySnapshotMigrationConflictError, match="changed"):
        migrate_legacy_registry(source, tmp_path / "data", store)

    assert [scope.name for scope in store.list_scopes()] == ["one"]
    assert [member.id for member in store.list_members()] == original_member_ids


def test_migration_marker_can_guard_runtime_startup_from_mutable_legacy_source(tmp_path):
    source = tmp_path / "registry.json"
    source.write_text(json.dumps([{"name": "one", "path": "/repos/one"}]))
    store = _store(tmp_path)

    assert legacy_registry_migrated(store) is False
    migrate_legacy_registry(source, tmp_path / "data", store)
    source.write_text("[]")

    assert legacy_registry_migrated(store) is True


def test_catalog_conflict_rolls_back_all_imported_rows_and_marker(tmp_path):
    source = tmp_path / "registry.json"
    source.write_text(
        json.dumps(
            [
                {"name": "new", "path": "/repos/new"},
                {"name": "existing", "path": "/repos/other"},
            ]
        )
    )
    store = _store(tmp_path)
    store.create_scope("existing")

    with pytest.raises(RegistrySnapshotMigrationConflictError, match="conflicts"):
        migrate_legacy_registry(source, tmp_path / "data", store)

    assert [scope.name for scope in store.list_scopes()] == ["existing"]
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='migration_markers'"
            ).fetchone()[0]
            == 0
        )


def test_missing_registry_is_noop_and_never_touches_unrelated_evolution_db(tmp_path):
    evolution = tmp_path / "data" / "evolution.db"
    evolution.parent.mkdir()
    evolution.write_bytes(b"do not touch")

    result = migrate_legacy_registry(tmp_path / "missing.json", tmp_path / "data", _store(tmp_path))

    assert result.status == "not_needed"
    assert evolution.read_bytes() == b"do not touch"
