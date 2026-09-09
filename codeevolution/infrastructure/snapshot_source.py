"""Manifest-limited source inventory for immutable snapshot artifacts."""

from __future__ import annotations

import fnmatch
import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


class SnapshotSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceEntry:
    path: str
    sha256: str
    size: int
    categories: tuple[str, ...]
    encoding_status: str = "unknown"


class SnapshotSourceInventory:
    """Read only files explicitly listed by an evidence manifest."""

    def __init__(self, artifact_root: str | Path, manifest: Mapping[str, Any]):
        self.root = Path(artifact_root).resolve()
        if not self.root.is_dir():
            raise SnapshotSourceError("snapshot artifact directory is unavailable")
        self._entries: dict[str, SourceEntry] = {}
        for raw in manifest.get("sources", []):
            path = _safe_relative_path(str(raw.get("path", "")))
            if path in self._entries:
                raise SnapshotSourceError(f"duplicate source manifest path: {path}")
            categories = raw.get("category", raw.get("categories", []))
            if isinstance(categories, str):
                categories = [categories]
            entry = SourceEntry(
                path=path,
                sha256=str(raw.get("sha256", "")),
                size=int(raw.get("size", -1)),
                categories=tuple(sorted(str(value) for value in categories)),
                encoding_status=str(raw.get("encoding_status", "unknown")),
            )
            if entry.size < 0:
                raise SnapshotSourceError(f"invalid source size: {path}")
            self._entries[path] = entry

    def list_files(
        self,
        categories: Iterable[str] | None = None,
        globs: Iterable[str] | None = None,
    ) -> list[SourceEntry]:
        required = set(categories or ())
        patterns = tuple(globs or ())
        return [
            entry
            for entry in sorted(self._entries.values(), key=lambda item: item.path)
            if (not required or required.intersection(entry.categories))
            and (not patterns or any(fnmatch.fnmatchcase(entry.path, pattern) for pattern in patterns))
        ]

    def read_bytes(self, path: str) -> bytes | None:
        try:
            normalized = _safe_relative_path(path)
        except SnapshotSourceError:
            return None
        entry = self._entries.get(normalized)
        if entry is None:
            return None
        candidate = self.root / normalized
        try:
            if self._has_symlink_component(candidate) or not candidate.is_file():
                return None
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
            raw = resolved.read_bytes()
        except (OSError, ValueError):
            return None
        if len(raw) != entry.size:
            return None
        expected = entry.sha256.removeprefix("sha256:")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            return None
        return raw

    def _has_symlink_component(self, candidate: Path) -> bool:
        relative = candidate.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return True
        return False

    def read_text(self, path: str) -> str | None:
        raw = self.read_bytes(path)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def snippet(self, path: str, start: int, end: int) -> str | None:
        if start < 1 or end < start:
            return None
        source = self.read_text(path)
        if source is None:
            return None
        lines = source.splitlines()
        if start > len(lines):
            return None
        return "\n".join(lines[start - 1 : min(end, len(lines))])


# Transitional name used in the technical design prose.
SnapshotSourceProvider = SnapshotSourceInventory


def _safe_relative_path(path: str) -> str:
    if not path or "\x00" in path:
        raise SnapshotSourceError("invalid source path")
    normalized = unicodedata.normalize("NFC", path.replace("\\", "/"))
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() in {"", "."}:
        raise SnapshotSourceError("unsafe source path")
    return pure.as_posix()
