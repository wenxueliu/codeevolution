"""Freeze and fingerprint a CodeGraph SQLite database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .workspace_input_scanner import InputEntry


class CodeGraphCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodeGraphCaptureResult:
    path: Path
    blob_sha256: str
    logical_graph_digest: str
    schema_fingerprint: str
    byte_size: int
    integrity_check: str = "ok"
    node_collision_count: int = 0


class CodeGraphCapture:
    REQUIRED_TABLES = frozenset({"nodes", "edges", "files"})

    def capture(
        self,
        source_db: str | Path,
        destination_db: str | Path,
        repository_root: str | Path | None = None,
    ) -> CodeGraphCaptureResult:
        source = Path(source_db).expanduser().resolve()
        destination = Path(destination_db).expanduser().resolve()
        graph_root = (
            Path(repository_root).expanduser().resolve()
            if repository_root is not None
            else (source.parent.parent if source.parent.name == ".codegraph" else source.parent)
        )
        if not source.is_file() or source.is_symlink():
            raise CodeGraphCaptureError("CodeGraph database is unavailable or unsafe")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise CodeGraphCaptureError("capture destination already exists")

        source_uri = f"file:{source.as_posix()}?mode=ro"
        try:
            with sqlite3.connect(source_uri, uri=True) as input_db:
                with sqlite3.connect(destination) as output_db:
                    input_db.backup(output_db)
            destination.chmod(0o600)
            with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                integrity_rows = [row[0] for row in db.execute("PRAGMA integrity_check")]
                if integrity_rows != ["ok"]:
                    raise CodeGraphCaptureError("captured CodeGraph failed integrity_check")
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                missing = self.REQUIRED_TABLES - tables
                if missing:
                    raise CodeGraphCaptureError(
                        f"captured CodeGraph missing tables: {', '.join(sorted(missing))}"
                    )
                schema = _schema_fingerprint(db)
                logical, collisions = _logical_graph_digest(db, graph_root)
        except sqlite3.Error as exc:
            destination.unlink(missing_ok=True)
            raise CodeGraphCaptureError("unable to capture CodeGraph database") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        blob = _file_sha256(destination)
        return CodeGraphCaptureResult(
            path=destination,
            blob_sha256=f"sha256:{blob}",
            logical_graph_digest=f"sha256:{logical}",
            schema_fingerprint=f"sha256:{schema}",
            byte_size=destination.stat().st_size,
            node_collision_count=collisions,
        )


def freeze_sources(
    repo_root: str | Path,
    entries: list[InputEntry] | tuple[InputEntry, ...],
    destination_root: str | Path,
) -> tuple[InputEntry, ...]:
    """Copy an already-observed source set while revalidating every byte.

    This is the E/F boundary used by the attempt worker: if any file differs
    from its observation, no partial frozen set is returned.
    """
    source_root = Path(repo_root).expanduser().resolve()
    target_root = Path(destination_root).expanduser().resolve()
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    frozen: list[InputEntry] = []
    seen: set[str] = set()
    try:
        for entry in sorted(entries, key=lambda item: item.path):
            relative = PurePosixPath(entry.path.replace("\\", "/"))
            normalized = relative.as_posix()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or normalized in {"", "."}
                or normalized in seen
            ):
                raise CodeGraphCaptureError("unsafe or duplicate source capture path")
            seen.add(normalized)
            try:
                source_fd = _open_beneath(source_root, relative)
            except OSError as exc:
                raise CodeGraphCaptureError(f"source unavailable during freeze: {entry.path}") from exc
            try:
                before = os.fstat(source_fd)
                output = target_root / relative
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                digest = hashlib.sha256()
                copied = 0
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        view = memoryview(chunk)
                        while view:
                            written = os.write(output_fd, view)
                            view = view[written:]
                        digest.update(chunk)
                        copied += len(chunk)
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                after = os.fstat(source_fd)
            finally:
                os.close(source_fd)
            actual_digest = f"sha256:{digest.hexdigest()}"
            if (
                copied != entry.size
                or actual_digest != entry.sha256
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise CodeGraphCaptureError(f"source changed during freeze: {entry.path}")
            frozen.append(entry)
    except Exception:
        # The attempt owns its staging tree and will ultimately reclaim it; remove
        # only files created by this helper so callers cannot consume a partial set.
        for path in sorted(target_root.rglob("*"), reverse=True):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        raise
    return tuple(frozen)


def _open_beneath(root: Path, relative: PurePosixPath) -> int:
    """Open a file without following a symlink in any path component."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    current_fd = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
    finally:
        os.close(current_fd)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\\", "/"))
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise CodeGraphCaptureError("non-finite value in CodeGraph metadata")
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')]


def _schema_fingerprint(db: sqlite3.Connection) -> str:
    schemas = []
    for table in sorted(CodeGraphCapture.REQUIRED_TABLES):
        rows = [dict(row) for row in db.execute(f'PRAGMA table_info("{table}")')]
        schemas.append({"table": table, "columns": rows})
    return hashlib.sha256(_json_bytes(schemas)).hexdigest()


def _logical_graph_digest(db: sqlite3.Connection, repository_root: Path) -> tuple[str, int]:
    node_columns = _table_columns(db, "nodes")
    node_fields = [
        field for field in ("kind", "qualified_name", "name", "file_path", "start_line", "signature")
        if field in node_columns
    ]
    if "id" not in node_columns:
        raise CodeGraphCaptureError("CodeGraph nodes table has no id")
    select = ", ".join(f'"{field}"' for field in ["id", *node_fields])
    node_rows = db.execute(f"SELECT {select} FROM nodes").fetchall()
    node_keys: dict[Any, tuple[Any, ...]] = {}
    counts: dict[bytes, int] = {}
    nodes: list[dict[str, Any]] = []
    for row in node_rows:
        semantic = {field: _canonical(row[field]) for field in node_fields}
        if "file_path" in semantic:
            semantic["file_path"] = _graph_path(semantic["file_path"], repository_root)
        key_bytes = _json_bytes(semantic)
        counts[key_bytes] = counts.get(key_bytes, 0) + 1
        node_keys[row["id"]] = tuple(semantic.get(field) for field in node_fields)
    for raw, count in counts.items():
        nodes.append({"semantic": json.loads(raw), "count": count})
    collisions = sum(count - 1 for count in counts.values() if count > 1)

    edge_columns = _table_columns(db, "edges")
    if not {"source", "target"}.issubset(edge_columns):
        raise CodeGraphCaptureError("CodeGraph edges table lacks source/target")
    edge_fields = [field for field in ("source", "target", "kind", "metadata", "line", "col", "provenance") if field in edge_columns]
    edge_select = ", ".join(f'"{field}"' for field in edge_fields)
    edges: list[dict[str, Any]] = []
    for row in db.execute(f"SELECT {edge_select} FROM edges"):
        item = {field: _canonical(row[field]) for field in edge_fields if field not in {"source", "target"}}
        if "metadata" in item and isinstance(item["metadata"], str):
            try:
                item["metadata"] = _canonical(json.loads(item["metadata"]))
            except json.JSONDecodeError:
                pass
        item["source"] = node_keys.get(row["source"], ("missing", str(row["source"])))
        item["target"] = node_keys.get(row["target"], ("missing", str(row["target"])))
        edges.append(item)

    file_columns = _table_columns(db, "files")
    file_fields = [field for field in ("path", "content_hash", "language", "size") if field in file_columns]
    file_select = ", ".join(f'"{field}"' for field in file_fields)
    files = [
        {
            field: _graph_path(row[field], repository_root)
            if field == "path"
            else _canonical(row[field])
            for field in file_fields
        }
        for row in db.execute(f"SELECT {file_select} FROM files")
    ]
    payload = {
        "nodes": sorted(nodes, key=_json_bytes),
        "edges": sorted(edges, key=_json_bytes),
        "files": sorted(files, key=_json_bytes),
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest(), collisions


def _graph_path(value: Any, repository_root: Path) -> Any:
    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    pure = Path(normalized)
    if pure.is_absolute():
        try:
            return pure.resolve(strict=False).relative_to(repository_root).as_posix()
        except ValueError:
            raise CodeGraphCaptureError("CodeGraph contains a path outside the repository") from None
    return PurePosixPath(normalized).as_posix()
