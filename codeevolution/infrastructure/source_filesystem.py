"""Filesystem-backed source provider."""

from pathlib import Path


class FileSystemSourceProvider:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path | None:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def read_text(self, path: str) -> str | None:
        candidate = self._resolve(path)
        if candidate is None:
            return None
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
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
