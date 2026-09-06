"""Versioned topology cache with backwards-compatible reads."""

import json
from pathlib import Path

from .registry_json import atomic_write_json

CACHE_SCHEMA_VERSION = 1


class TopologyCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        value.setdefault("schema_version", 0)
        return value

    def save(self, value: dict) -> None:
        atomic_write_json(self.path, {**value, "schema_version": CACHE_SCHEMA_VERSION})
