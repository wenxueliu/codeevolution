"""JSON registry persistence with atomic replacement."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..platform import (
    atomic_replace,
    ensure_supported_storage_path,
    fsync_directory,
    fsync_file,
    set_private_permissions,
)


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_supported_storage_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, default=str)
            stream.flush()
            fsync_file(stream)
        set_private_permissions(temporary)
        atomic_replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RegistryRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def save(self, entries: list[dict]) -> None:
        atomic_write_json(self.path, entries)
