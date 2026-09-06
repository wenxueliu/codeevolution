"""Architectural-layer classification and violation detection."""

from collections.abc import Callable
from typing import Any, Protocol

from ...domain.knowledge import LayerViolation

LAYER_PATTERNS = [
    (name, "presentation") for name in (
        "controller", "handler", "view", "route", "router", "middleware", "api",
        "endpoint", "resource", "serializer",
    )
] + [
    (name, "application") for name in ("service", "usecase", "use_case", "interactor")
] + [
    (name, "domain") for name in (
        "domain", "model", "entity", "valueobject", "value_object", "aggregate",
    )
] + [
    (name, "infrastructure") for name in (
        "repository", "dao", "mapper", "persistence", "database", "db", "client",
        "gateway", "adapter", "config", "configuration",
    )
] + [(name, "test") for name in ("tests", "test", "__tests__", "spec", "fixtures", "e2e", "integration")]

LAYER_ALLOWED = {
    ("presentation", "application"): True, ("presentation", "domain"): True,
    ("presentation", "infrastructure"): False, ("application", "domain"): True,
    ("application", "infrastructure"): True, ("domain", "presentation"): False,
    ("domain", "application"): False, ("domain", "infrastructure"): True,
    ("infrastructure", "presentation"): False, ("infrastructure", "application"): False,
    ("infrastructure", "domain"): False,
}


class _Source(Protocol):
    def get_all_files(self) -> list[str]: ...
    def layer_call_edges(self) -> list[dict[str, Any]]: ...


class LayerRuleExtractor:
    def __init__(self, source: _Source | Callable[[], list[LayerViolation]]):
        self._source = source

    def extract(self) -> list[LayerViolation]:
        if callable(self._source) and not hasattr(self._source, "layer_call_edges"):
            return self._source()
        file_layers = {
            path: self.classify_file(path)
            for path in self._source.get_all_files()  # type: ignore[union-attr]
        }
        rows = self._source.layer_call_edges()  # type: ignore[union-attr]
        violations = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            pair = (row["source_file"], row["target_file"])
            source_layer = file_layers.get(pair[0], "")
            target_layer = file_layers.get(pair[1], "")
            if (
                pair in seen or not source_layer or not target_layer
                or source_layer == target_layer or "test" in (source_layer, target_layer)
                or LAYER_ALLOWED.get((source_layer, target_layer)) is not False
            ):
                continue
            seen.add(pair)
            violations.append(LayerViolation(
                source_name=row["source_name"], source_file=pair[0], source_layer=source_layer,
                target_name=row["target_name"], target_file=pair[1], target_layer=target_layer,
                call_line=row.get("call_line"),
            ))
        return violations

    @staticmethod
    def classify_file(file_path: str) -> str:
        segments = file_path.lower().replace("\\", "/").split("/")
        for pattern, layer in LAYER_PATTERNS:
            if any(
                segment == pattern or segment.startswith(f"{pattern}_")
                or segment.endswith(f"_{pattern}") for segment in segments
            ):
                return layer
        return ""
