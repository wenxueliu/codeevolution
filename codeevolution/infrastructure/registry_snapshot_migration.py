"""One-time migration from the legacy JSON registry to the snapshot catalog.

The legacy :mod:`codeevolution.registry` facade intentionally is not used here:
its repository adapter treats unreadable JSON as an empty registry.  Migration
must be loss-intolerant, retain the source bytes, and import the complete
catalog together with its marker in one SQLite transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from codeevolution.infrastructure.analysis_snapshot_sqlite import AnalysisSnapshotSQLiteStore

MIGRATION_NAME = "legacy-json-registry-v1"
_ID_NAMESPACE = UUID("84a130e5-e7cb-4c8b-94b4-e71f11933f00")


class RegistrySnapshotMigrationError(ValueError):
    """The legacy registry cannot be migrated without losing information."""


class RegistrySnapshotMigrationConflictError(RegistrySnapshotMigrationError):
    """The one-time migration was already completed for different input."""


@dataclass(frozen=True)
class RegistryMigrationResult:
    status: Literal["not_needed", "migrated", "already_migrated"]
    source_sha256: str | None
    scope_count: int
    member_count: int
    backup_path: Path | None


@dataclass(frozen=True)
class _Member:
    display_name: str
    registered_path: str
    path_identity: str


@dataclass(frozen=True)
class _Scope:
    name: str
    members: tuple[_Member, ...]


def legacy_registry_migrated(store: AnalysisSnapshotSQLiteStore) -> bool:
    """Return whether the one-time registry import has already committed.

    Runtime startup uses this guard because the legacy JSON remains writable
    during the compatibility window.  The strict migration function below is
    still useful for explicit migration/audit calls, where changed source bytes
    must be reported as a conflict.
    """

    with store.connection() as connection:
        table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='migration_markers'"""
        ).fetchone()
        if table is None:
            return False
        marker = connection.execute(
            "SELECT 1 FROM migration_markers WHERE name=?", (MIGRATION_NAME,)
        ).fetchone()
    return marker is not None


def migrate_legacy_registry(
    registry_path: str | Path,
    data_dir: str | Path,
    store: AnalysisSnapshotSQLiteStore,
) -> RegistryMigrationResult:
    """Strictly import a legacy registry into ``store`` exactly once.

    A missing source means there is no legacy state to migrate.  Every present
    source, including an empty registry, is backed up and marked.  Once marked,
    only the exact same source bytes are accepted on subsequent calls.
    """

    source = Path(registry_path)
    destination = Path(data_dir)
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        return RegistryMigrationResult("not_needed", None, 0, 0, None)
    except OSError as exc:
        raise RegistrySnapshotMigrationError(
            f"cannot read legacy registry {source}: {exc}"
        ) from exc

    source_digest = sha256(raw).hexdigest()
    scopes = _parse_registry(raw, source)
    backup = _backup_registry(raw, source_digest, destination)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    with store.connection() as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS migration_markers (
                    name TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    scope_count INTEGER NOT NULL CHECK(scope_count >= 0),
                    member_count INTEGER NOT NULL CHECK(member_count >= 0)
                )"""
            )
            previous = connection.execute(
                "SELECT * FROM migration_markers WHERE name=?", (MIGRATION_NAME,)
            ).fetchone()
            if previous is not None:
                if previous["source_sha256"] != source_digest:
                    raise RegistrySnapshotMigrationConflictError(
                        "legacy registry changed after its one-time migration "
                        f"(recorded {previous['source_sha256']}, current {source_digest})"
                    )
                connection.commit()
                return RegistryMigrationResult(
                    "already_migrated",
                    source_digest,
                    previous["scope_count"],
                    previous["member_count"],
                    Path(previous["backup_path"]),
                )

            scope_count, member_count = _insert_catalog(connection, scopes, source_digest, now)
            connection.execute(
                """INSERT INTO migration_markers
                   (name,source_path,source_sha256,completed_at,backup_path,
                    scope_count,member_count)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    MIGRATION_NAME,
                    str(source.resolve()),
                    source_digest,
                    now,
                    str(backup),
                    scope_count,
                    member_count,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    return RegistryMigrationResult("migrated", source_digest, scope_count, member_count, backup)


def _parse_registry(raw: bytes, source: Path) -> tuple[_Scope, ...]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrySnapshotMigrationError(
            f"legacy registry {source} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(value, list):
        raise RegistrySnapshotMigrationError("legacy registry root must be a JSON array")

    scopes: list[_Scope] = []
    scope_names: set[str] = set()
    paths: dict[str, str] = {}
    basenames: dict[str, str] = {}
    for scope_index, entry in enumerate(value):
        location = f"registry[{scope_index}]"
        if not isinstance(entry, dict):
            raise RegistrySnapshotMigrationError(f"{location} must be an object")
        scope_name = _required_text(entry, "name", location)
        if scope_name in scope_names:
            raise RegistrySnapshotMigrationError(
                f"duplicate scope name {scope_name!r} at {location}"
            )
        scope_names.add(scope_name)

        repositories = entry.get("repositories")
        if repositories is not None and not isinstance(repositories, list):
            raise RegistrySnapshotMigrationError(f"{location}.repositories must be an array")
        if repositories:
            raw_members = repositories
        else:
            # Empty ``repositories`` was historically treated as single-repo
            # form, so retain that behavior while still validating its path.
            raw_members = [{"name": scope_name, "path": entry.get("path")}]

        members: list[_Member] = []
        member_names: set[str] = set()
        for member_index, member in enumerate(raw_members):
            member_location = f"{location}.repositories[{member_index}]"
            if not isinstance(member, dict):
                raise RegistrySnapshotMigrationError(f"{member_location} must be an object")
            raw_path = _required_text(member, "path", member_location)
            normalized_path = str(Path(raw_path).expanduser().resolve())
            path_identity = os.path.normcase(normalized_path)
            basename = os.path.normcase(Path(normalized_path).name)
            display_name_value = member.get("name")
            if display_name_value is None:
                display_name = Path(normalized_path).name
            elif isinstance(display_name_value, str) and display_name_value.strip():
                display_name = display_name_value.strip()
            else:
                raise RegistrySnapshotMigrationError(
                    f"{member_location}.name must be a non-empty string when present"
                )
            if display_name in member_names:
                raise RegistrySnapshotMigrationError(
                    f"duplicate member name {display_name!r} in scope {scope_name!r}"
                )
            member_names.add(display_name)
            if path_identity in paths:
                raise RegistrySnapshotMigrationError(
                    f"duplicate repository path {normalized_path!r}; first used by {paths[path_identity]}"
                )
            if basename in basenames:
                raise RegistrySnapshotMigrationError(
                    f"duplicate repository basename {Path(normalized_path).name!r}; "
                    f"also used by {basenames[basename]}"
                )
            paths[path_identity] = f"{scope_name}/{display_name}"
            basenames[basename] = normalized_path
            members.append(_Member(display_name, normalized_path, path_identity))
        scopes.append(_Scope(scope_name, tuple(members)))
    return tuple(scopes)


def _required_text(value: dict[str, Any], key: str, location: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise RegistrySnapshotMigrationError(f"{location}.{key} must be a non-empty string")
    return item.strip()


def _backup_registry(raw: bytes, source_digest: str, data_dir: Path) -> Path:
    directory = data_dir / "migration-backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    # Digest naming makes retries before marker commit converge on one backup,
    # while retaining the design's registry-* naming convention.
    backup = directory / f"registry-{source_digest}.json"
    if backup.exists():
        if sha256(backup.read_bytes()).hexdigest() != source_digest:
            raise RegistrySnapshotMigrationError(f"existing registry backup is corrupt: {backup}")
        return backup

    descriptor, temporary_name = tempfile.mkstemp(prefix=".registry-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        # Another process may have won this race with identical source bytes;
        # replacing it is harmless because the filename is content-addressed.
        os.replace(temporary, backup)
        os.chmod(backup, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def _insert_catalog(
    connection: sqlite3.Connection,
    scopes: tuple[_Scope, ...],
    source_digest: str,
    timestamp: str,
) -> tuple[int, int]:
    member_count = 0
    try:
        for scope in scopes:
            scope_id = str(uuid5(_ID_NAMESPACE, f"{source_digest}:scope:{scope.name}"))
            connection.execute(
                "INSERT INTO analysis_scopes(id,name,created_at) VALUES(?,?,?)",
                (scope_id, scope.name, timestamp),
            )
            for member in scope.members:
                logical_identity = (
                    f"{source_digest}:member:{scope.name}:"
                    f"{member.display_name}:{member.path_identity}"
                )
                connection.execute(
                    """INSERT INTO repository_members
                       (id,scope_id,display_name,registered_path,path_identity,
                        created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid5(_ID_NAMESPACE, logical_identity)),
                        scope_id,
                        member.display_name,
                        member.registered_path,
                        member.path_identity,
                        timestamp,
                        timestamp,
                    ),
                )
                member_count += 1
    except sqlite3.IntegrityError as exc:
        raise RegistrySnapshotMigrationConflictError(
            f"legacy registry conflicts with the snapshot catalog: {exc}"
        ) from exc
    return len(scopes), member_count
