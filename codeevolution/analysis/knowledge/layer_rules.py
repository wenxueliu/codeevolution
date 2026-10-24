"""Architectural-layer classification and violation detection."""

from collections.abc import Callable
from typing import Any, Protocol

from ...domain.knowledge import LayerViolation
from .layer_config import (
    LEGACY_ALLOW,
    LEGACY_KEYWORDS,
    LayerClassifier,
    LayerConfig,
    builtin_layers_config,
    evaluate_layer_edges,
)

# Kept importable for callers that still read the legacy tables directly.  The
# live policy now comes from ``LayerConfig``; these are only its historical
# rendering.
LAYER_PATTERNS = [
    (name, layer) for layer, names in LEGACY_KEYWORDS.items() for name in names
]

LAYER_ALLOWED = {pair: True for pair in LEGACY_ALLOW} | {
    pair: False
    for pair in (
        ("presentation", "infrastructure"),
        ("domain", "presentation"),
        ("domain", "application"),
        ("infrastructure", "presentation"),
        ("infrastructure", "application"),
        ("infrastructure", "domain"),
    )
}


class _Source(Protocol):
    def get_all_files(self) -> list[str]: ...
    def layer_call_edges(self) -> list[dict[str, Any]]: ...


class LayerRuleExtractor:
    """Detect cross-layer calls under a declarative ``LayerConfig``."""

    def __init__(
        self,
        source: _Source | Callable[[], list[LayerViolation]],
        config: LayerConfig | None = None,
    ):
        self._source = source
        self.config = config or builtin_layers_config()

    @property
    def classifier(self) -> LayerClassifier:
        return self.config.classifier

    def extract(self) -> list[LayerViolation]:
        if callable(self._source) and not hasattr(self._source, "layer_call_edges"):
            return self._source()
        return evaluate_layer_edges(
            self._source.layer_call_edges(),  # type: ignore[union-attr]
            self.config.classifier,
            self.config,
        )

    def classify_file(self, file_path: str) -> str:
        return self.config.classifier.classify(file_path)
