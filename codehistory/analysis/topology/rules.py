"""Configurable framework rule sets for static topology analysis."""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TopologyRuleSet:
    version: str = "v1"
    http_client_callers: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    database_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    message_queue_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: str | Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            version=data.get("version", "custom"),
            http_client_callers={
                language: [(item[0], item[1]) for item in patterns]
                for language, patterns in data.get("http_client_callers", {}).items()
            },
            database_patterns={
                key: tuple(value) for key, value in data.get("database_patterns", {}).items()
            },
            message_queue_patterns={
                key: tuple(value) for key, value in data.get("message_queue_patterns", {}).items()
            },
        )
