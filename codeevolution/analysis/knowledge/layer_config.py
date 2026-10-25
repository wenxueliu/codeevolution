"""Declarative architecture layers: configuration, matching and evaluation.

Layers belong to a repository, not to a language.  A Java MVC tree, a Python
hexagonal tree and a Go module layout name the same concerns through different
path conventions, so no built-in table can classify all three.  This module
turns the layer declaration into repository data -- glob patterns plus an
allow/deny edge policy -- and keeps the previous hardcoded table as the
fallback so an undeclared repository keeps producing the results it produced
before.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.knowledge import LayerViolation

logger = logging.getLogger(__name__)

LAYERS_VERSION = "layers/v1"
CONFIG_RELPATH = ".codeevolution/layers.json"
BASELINE_RELPATH = ".codeevolution/layers.baseline.json"

# Canonical names ``layers init`` prefers when a discovered directory token
# matches the glossary.  Nothing at check time depends on this order; it only
# keeps generated files readable and stable.
CANONICAL_ORDER = ("presentation", "application", "domain", "infrastructure", "test")

# The directory-name table the classifier used before layers became
# configurable.  It stays here as the built-in default so an undeclared
# repository is classified exactly as it was.
LEGACY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "presentation": (
        "controller", "handler", "view", "route", "router", "middleware", "api",
        "endpoint", "resource", "serializer",
    ),
    "application": ("service", "usecase", "use_case", "interactor"),
    "domain": ("domain", "model", "entity", "valueobject", "value_object", "aggregate"),
    "infrastructure": (
        "repository", "dao", "mapper", "persistence", "database", "db", "client",
        "gateway", "adapter", "config", "configuration",
    ),
    "test": ("tests", "test", "__tests__", "spec", "fixtures", "e2e", "integration"),
}

# The pairs the previous hardcoded matrix rejected.  Everything else passed.
LEGACY_DENY: tuple[tuple[str, str], ...] = (
    ("presentation", "infrastructure"),
    ("domain", "presentation"),
    ("domain", "application"),
    ("infrastructure", "presentation"),
    ("infrastructure", "application"),
    ("infrastructure", "domain"),
)

LEGACY_ALLOW: tuple[tuple[str, str], ...] = (
    ("presentation", "application"),
    ("presentation", "domain"),
    ("application", "domain"),
    ("application", "infrastructure"),
    ("domain", "infrastructure"),
)

# Extra tokens ``layers init`` recognises when seeding a configuration.  This is
# a proposal vocabulary, never a rule: the generated file lists plain globs and
# is edited by hand afterwards.
_EXTRA_ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "presentation": (
        "presentation", "controllers", "handlers", "views", "routes", "routers", "apis",
        "endpoints", "resource", "serializers", "rest", "http", "servlets", "delivery",
        "interface", "interfaces",
    ),
    "application": (
        "application", "services", "usecases", "use_cases", "interactors", "commands", "queries",
    ),
    "domain": ("domains", "models", "entities", "aggregates", "core", "business"),
    "infrastructure": (
        "infrastructure", "infra", "repositories", "repo", "repos", "daos", "mappers",
        "persistence", "database", "clients", "gateways", "adapters", "configuration",
        "migrations", "storage", "drivers", "providers", "external",
    ),
    "test": ("specs", "integration_tests"),
}


def _build_glossary() -> dict[str, str]:
    glossary: dict[str, str] = {}
    for layer in CANONICAL_ORDER:
        for token in LEGACY_KEYWORDS[layer] + _EXTRA_ROLE_TOKENS.get(layer, ()):
            glossary.setdefault(token.lower(), layer)
    return glossary


ROLE_GLOSSARY: dict[str, str] = _build_glossary()


def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a repository-relative glob into an anchored regex.

    ``**/`` matches zero or more leading directories, ``**`` matches anything
    including separators, and ``*`` stops at a separator.  Matching is
    case-sensitive because layer patterns are written against real paths.
    """
    out: list[str] = []
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                index += 2
                if index < length and pattern[index] == "/":
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _specificity(pattern: str) -> int:
    return sum(1 for char in pattern if char not in "*?")


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


@dataclass(frozen=True)
class LayerPattern:
    layer: str
    glob: str
    regex: re.Pattern[str]
    specificity: int
    order: int

    @property
    def rank(self) -> tuple[int, int, int]:
        """More literal characters win; then a longer pattern; then declaration order."""
        return (self.specificity, len(self.glob), -self.order)


class LayerClassifier:
    """Map a repository-relative path to one declared layer, or ``""``."""

    def __init__(self, layers: Mapping[str, Iterable[str]]):
        patterns: list[LayerPattern] = []
        for order, (layer, globs) in enumerate(layers.items()):
            for glob in globs:
                if not glob.strip():
                    continue
                patterns.append(
                    LayerPattern(
                        layer=str(layer),
                        glob=glob,
                        regex=_compile_glob(normalize_path(glob)),
                        specificity=_specificity(glob),
                        order=order,
                    )
                )
        self._patterns = tuple(patterns)

    def classify(self, path: str) -> str:
        normalized = normalize_path(path)
        if not normalized:
            return ""
        best_layer, best_rank = "", None
        for pattern in self._patterns:
            if not pattern.regex.match(normalized):
                continue
            if best_rank is None or pattern.rank > best_rank:
                best_rank, best_layer = pattern.rank, pattern.layer
        return best_layer

    def classify_all(self, paths: Iterable[str]) -> dict[str, str]:
        return {path: self.classify(path) for path in paths}


@dataclass(frozen=True)
class LayerConfig:
    """A repository's layer declaration plus its dependency policy."""

    layers: Mapping[str, tuple[str, ...]]
    allow: frozenset[tuple[str, str]] = frozenset()
    deny: frozenset[tuple[str, str]] = frozenset()
    strict: bool = True
    ignore: frozenset[str] = frozenset()
    origin: str = "builtin"

    def __post_init__(self) -> None:
        declared = set(self.layers)
        # ``ignore`` silently drops undeclared names so a generated config can
        # always carry the canonical ignore list even when a layer was never
        # observed in that repository.
        object.__setattr__(self, "ignore", frozenset(self.ignore & declared))
        for pair in sorted(self.allow | self.deny):
            unknown = [name for name in pair if name not in declared]
            if unknown:
                raise ValueError(
                    f"layers config {self.origin!r} references undeclared layer "
                    f"{unknown[0]!r} in rule {pair[0]} -> {pair[1]}"
                )
        object.__setattr__(self, "_classifier", LayerClassifier(self.layers))

    @property
    def classifier(self) -> LayerClassifier:
        return self._classifier  # type: ignore[attr-defined]

    @property
    def ignored_layers(self) -> frozenset[str]:
        return self.ignore

    def violation(self, source_layer: str, target_layer: str) -> bool:
        if not source_layer or not target_layer:
            return False
        if source_layer == target_layer:
            return False
        if source_layer in self.ignore or target_layer in self.ignore:
            return False
        pair = (source_layer, target_layer)
        if pair in self.deny:
            return True
        if pair in self.allow:
            return False
        return self.strict

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": LAYERS_VERSION,
            "layers": {name: list(globs) for name, globs in self.layers.items()},
            "rules": {
                "strict": self.strict,
                "allow": [list(pair) for pair in sorted(self.allow)],
                "deny": [list(pair) for pair in sorted(self.deny)],
                "ignore": sorted(self.ignore),
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], origin: str = "config") -> LayerConfig:
        version = str(data.get("version") or LAYERS_VERSION)
        if version != LAYERS_VERSION:
            raise ValueError(f"unsupported layers config version: {version!r}")
        raw_layers = data.get("layers")
        if not isinstance(raw_layers, Mapping) or not raw_layers:
            raise ValueError("layers config must declare at least one layer")
        layers: dict[str, tuple[str, ...]] = {}
        for name, globs in raw_layers.items():
            if not isinstance(globs, (list, tuple)) or not all(
                isinstance(glob, str) for glob in globs
            ):
                raise ValueError(f"layer {name!r} must list glob strings")
            layers[str(name)] = tuple(globs)
        rules = data.get("rules") or {}
        if not isinstance(rules, Mapping):
            raise ValueError("rules must be a JSON object")
        return cls(
            layers=layers,
            allow=_pairs(rules.get("allow"), "allow"),
            deny=_pairs(rules.get("deny"), "deny"),
            strict=bool(rules.get("strict", True)),
            ignore=frozenset(str(name) for name in (rules.get("ignore") or ())),
            origin=origin,
        )


def _pairs(raw: Any, key: str) -> frozenset[tuple[str, str]]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"rules.{key} must be a list of [source, target] pairs")
    pairs: set[tuple[str, str]] = set()
    for item in raw:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not all(isinstance(name, str) for name in item)
        ):
            raise ValueError(f"rules.{key} entry must be [source, target]: {item!r}")
        pairs.add((item[0], item[1]))
    return frozenset(pairs)


def _legacy_globs(keywords: Iterable[str]) -> tuple[str, ...]:
    globs: list[str] = []
    for keyword in keywords:
        globs.extend((f"**/{keyword}/**", f"**/{keyword}_*/**", f"**/*_{keyword}/**"))
    return tuple(globs)


BUILTIN_LAYERS: dict[str, tuple[str, ...]] = {
    layer: _legacy_globs(LEGACY_KEYWORDS[layer]) for layer in CANONICAL_ORDER
}


def builtin_layers_config() -> LayerConfig:
    """The behaviour-preserving default for repositories without their own config."""
    return LayerConfig(
        layers=BUILTIN_LAYERS,
        allow=frozenset(LEGACY_ALLOW),
        deny=frozenset(LEGACY_DENY),
        strict=False,
        ignore=frozenset({"test"}),
        origin="builtin",
    )


def load_layers_config(path: str | Path) -> LayerConfig:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{config_path}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{config_path}: layers config must be a JSON object")
    return LayerConfig.from_dict(data, origin=str(config_path))


def load_repo_layers_config(repo_root: str | Path) -> LayerConfig:
    """Load a repository's declaration, falling back to the built-in defaults.

    A broken config is reported and then ignored rather than aborting the whole
    knowledge extraction; ``codeevolution layers check`` surfaces the same error
    as a hard failure.
    """
    path = Path(repo_root) / CONFIG_RELPATH
    if not path.exists():
        return builtin_layers_config()
    try:
        return load_layers_config(path)
    except ValueError as error:
        logger.warning("ignoring invalid layers config: %s", error)
        return builtin_layers_config()


def evaluate_layer_edges(
    edges: Iterable[Mapping[str, Any]],
    classifier: LayerClassifier,
    config: LayerConfig,
) -> list[LayerViolation]:
    """Turn cross-file call edges into layer violations under ``config``."""
    violations: list[LayerViolation] = []
    seen: set[tuple[str, str]] = set()
    for row in edges:
        source_file = row.get("source_file") or ""
        target_file = row.get("target_file") or ""
        pair = (source_file, target_file)
        if pair in seen:
            continue
        source_layer = classifier.classify(source_file)
        target_layer = classifier.classify(target_file)
        if not config.violation(source_layer, target_layer):
            continue
        seen.add(pair)
        violations.append(
            LayerViolation(
                source_name=row.get("source_name") or "",
                source_file=source_file,
                source_layer=source_layer,
                target_name=row.get("target_name") or "",
                target_file=target_file,
                target_layer=target_layer,
                call_line=row.get("call_line"),
            )
        )
    return sorted(violations, key=lambda item: (item.source_file, item.target_file))
