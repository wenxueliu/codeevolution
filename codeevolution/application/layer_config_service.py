"""Generate and check a repository's layer declaration from its code graph.

``layers init`` answers one question: what layering does this repository
*already* have?  It reads the cross-file call edges, names the layers it can
recognise, and writes every observed dependency direction as an allowed edge.
The generated file therefore passes on the tree it came from -- from then on a
new direction fails until someone edits the declaration.  That is the ratchet:
freeze what exists, then narrow it pair by pair.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..analysis.knowledge.layer_config import (
    CANONICAL_ORDER,
    LAYERS_VERSION,
    ROLE_GLOSSARY,
    LayerConfig,
    evaluate_layer_edges,
    normalize_path,
)
from ..domain.knowledge import LayerViolation

BASELINE_VERSION = "layers-baseline/v1"

_NAME_TOKENS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Directories that wrap a source tree without naming a concern.  Only the
# module-boundary fallback needs them; role discovery ignores them naturally.
_WRAPPER_DIRS = {
    "src", "lib", "libs", "app", "internal", "pkg", "source", "sources", "main",
    "java", "kotlin", "scala", "python", "go", "cmd", "com", "org", "net", "io",
}

# A filename suffix earns a layer only when it repeats: one ``core_entities.py``
# is a module name, twenty ``*Controller.java`` are a convention.
_SUFFIX_MIN_OCCURRENCES = 3
_SUFFIX_MIN_DIRECTORIES = 2

# Directions the previous built-in matrix already treated as violations.  They
# are only a ranking hint for ``layers init``: a reviewer should look at these
# pairs before the ones that merely exist.
_CONVENTION_BREAKS = {
    ("presentation", "infrastructure"),
    ("domain", "presentation"),
    ("domain", "application"),
    ("infrastructure", "presentation"),
    ("infrastructure", "application"),
    ("infrastructure", "domain"),
}


def _ordered(names: Iterable[str]) -> list[str]:
    known = [name for name in CANONICAL_ORDER if name in set(names)]
    return known + sorted(set(names) - set(known))


def _directory_role(path: str) -> tuple[str, str]:
    """Nearest role directory: ``com/mall/service/impl/X.java`` -> ``application``."""
    parts = normalize_path(path).split("/")[:-1]
    for segment in reversed(parts):
        key = segment.lower()
        layer = ROLE_GLOSSARY.get(key)
        if layer:
            return layer, key
    return "", ""


def _suffix_role(stem: str) -> tuple[str, str]:
    """Trailing role token: ``PmsProductServiceImpl`` -> ``("application", "ServiceImpl")``."""
    tokens = _NAME_TOKENS.findall(stem) or [stem]
    for size in (2, 1):
        if len(tokens) <= size - 1:
            continue
        candidate = "".join(tokens[-size:])
        layer = ROLE_GLOSSARY.get(candidate.lower())
        if layer:
            return layer, candidate
    return "", ""


def _discover_layers(files: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Derive layer globs from the role directories and filename suffixes present.

    A filename suffix is only treated as a layer when it repeats across more
    than one directory.  A single ``core_entities.py`` is a module name, not a
    convention; ``*Controller.java`` repeated across ``web/`` and ``admin/`` is.
    """
    paths = [normalize_path(path) for path in files]
    directories: dict[str, set[str]] = {}
    suffixes: dict[str, set[str]] = {}
    suffix_dirs: dict[str, set[str]] = {}
    suffix_counts: Counter[str] = Counter()
    for path in paths:
        layer, token = _directory_role(path)
        if layer:
            directories.setdefault(layer, set()).add(token)
            continue
        stem = PurePosixPath(path).stem
        layer, suffix = _suffix_role(stem)
        if not layer:
            continue
        suffixes.setdefault(layer, set()).add(suffix)
        suffix_counts[suffix] += 1
        suffix_dirs.setdefault(suffix, set()).add(str(PurePosixPath(path).parent))

    for layer, family in suffixes.items():
        suffixes[layer] = {
            suffix
            for suffix in family
            if suffix_counts[suffix] >= _SUFFIX_MIN_OCCURRENCES
            and len(suffix_dirs.get(suffix, ())) >= _SUFFIX_MIN_DIRECTORIES
        }

    layers: dict[str, tuple[str, ...]] = {}
    for layer in _ordered(list(directories) + [name for name, kept in suffixes.items() if kept]):
        globs = [f"**/{token}/**" for token in sorted(directories.get(layer, ()))]
        globs += [f"**/*{suffix}.*" for suffix in sorted(suffixes.get(layer, ()))]
        if globs:
            layers[layer] = tuple(dict.fromkeys(globs))
    return layers


def _fallback_layers(files: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """No recognisable role naming: fall back to the repository's module boundaries."""
    counts: Counter[str] = Counter()
    for path in files:
        directories = normalize_path(path).split("/")[:-1]
        if not directories:
            continue
        depth = 0
        while depth < len(directories) and directories[depth].lower() in _WRAPPER_DIRS:
            depth += 1
        if depth >= len(directories):
            depth = len(directories) - 1
        counts[directories[depth]] += 1
    names = [name for name, _ in counts.most_common(12)]
    return {name: (f"**/{name}/**",) for name in sorted(names)}


def _against_convention(source_layer: str, target_layer: str) -> bool:
    return (source_layer, target_layer) in _CONVENTION_BREAKS


class LayerConfigService:
    """Generate a layer declaration, or check a code graph against one."""

    def __init__(self, source: Any):
        self._source = source

    # -- generation -----------------------------------------------------

    def generate(self, max_files_per_pair: int = 3, max_samples: int = 8) -> dict[str, Any]:
        files = self._source.get_all_files()
        layers = _discover_layers(files)
        strategy = "role-naming"
        if not layers:
            layers = _fallback_layers(files)
            strategy = "module-boundary"

        draft = LayerConfig(
            layers=layers,
            allow=frozenset(),
            deny=frozenset(),
            strict=True,
            ignore=frozenset({"test"}),
            origin="generated",
        )
        classifier = draft.classifier
        classified = {path: classifier.classify(path) for path in files}
        covered = sum(1 for layer in classified.values() if layer)

        rows = self._source.layer_call_edges()
        observed: Counter[tuple[str, str]] = Counter()
        examples: dict[tuple[str, str], list[list[str]]] = {}
        unclassified = 0
        for row in rows:
            source_layer = classifier.classify(row.get("source_file") or "")
            target_layer = classifier.classify(row.get("target_file") or "")
            if not source_layer or not target_layer:
                unclassified += 1
                continue
            if source_layer == target_layer:
                continue
            if source_layer in draft.ignore or target_layer in draft.ignore:
                continue
            pair = (source_layer, target_layer)
            observed[pair] += 1
            bucket = examples.setdefault(pair, [])
            if len(bucket) < max_files_per_pair:
                bucket.append([row.get("source_file"), row.get("target_file")])

        config = LayerConfig(
            layers=layers,
            allow=frozenset(observed),
            deny=frozenset(),
            strict=True,
            ignore=frozenset({"test"}),
            origin="generated",
        )
        payload = config.to_dict()
        payload["generated"] = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy,
            "file_count": len(files),
            "classified_files": covered,
            "coverage": round(covered / len(files), 3) if files else 0.0,
            "edge_count": len(rows),
            "classified_edges": sum(observed.values()),
            "unclassified_edges": unclassified,
            "unclassified_samples": [
                path for path, layer in classified.items() if not layer
            ][:max_samples],
        }
        payload["evidence"] = {
            "observed": [
                {
                    "from": source_layer,
                    "to": target_layer,
                    "edges": count,
                    "files": examples.get((source_layer, target_layer), []),
                }
                for (source_layer, target_layer), count in sorted(
                    observed.items(), key=lambda item: (-item[1], item[0])
                )
            ]
        }
        payload["ratchet"] = {
            "note": (
                "Every listed pair is allowed because it exists today. Remove a pair from "
                "rules.allow to start flagging its violations; run 'layers check "
                "--write-baseline' first to grandfather the existing ones."
            ),
            "remove_candidates": self._remove_candidates(observed),
        }
        return payload

    @staticmethod
    def _remove_candidates(observed: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
        candidates = [
            {
                "from": source_layer,
                "to": target_layer,
                "edges": count,
                "against_convention": _against_convention(source_layer, target_layer),
            }
            for (source_layer, target_layer), count in observed.items()
        ]
        # Convention-breaking edges first, then the cheapest to fix.
        candidates.sort(key=lambda item: (not item["against_convention"], item["edges"], item["from"]))
        return candidates

    # -- checking -------------------------------------------------------

    def check(
        self, config: LayerConfig, baseline: frozenset[tuple[str, str]] = frozenset()
    ) -> dict[str, Any]:
        violations = evaluate_layer_edges(
            self._source.layer_call_edges(), config.classifier, config
        )
        known: list[LayerViolation] = []
        new: list[LayerViolation] = []
        for item in violations:
            pair = (item.source_file, item.target_file)
            (known if pair in baseline else new).append(item)
        return {
            "total": len(violations),
            "baseline": len(known),
            "new": new,
            "known": known,
            "groups": self._groups(new),
        }

    @staticmethod
    def _groups(violations: list[LayerViolation]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[LayerViolation]] = {}
        for item in violations:
            grouped.setdefault((item.source_layer, item.target_layer), []).append(item)
        return [
            {"from": source_layer, "to": target_layer, "count": len(items), "items": items}
            for (source_layer, target_layer), items in sorted(
                grouped.items(), key=lambda entry: (-len(entry[1]), entry[0])
            )
        ]


def read_baseline(path: str | Path) -> frozenset[tuple[str, str]]:
    baseline_path = Path(path)
    if not baseline_path.exists():
        return frozenset()
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    pairs = data.get("pairs") or []
    return frozenset((str(item[0]), str(item[1])) for item in pairs)


def write_baseline(path: str | Path, violations: Iterable[LayerViolation]) -> int:
    pairs = sorted({(item.source_file, item.target_file) for item in violations})
    payload = {
        "version": BASELINE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": [list(pair) for pair in pairs],
    }
    baseline_path = Path(path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return len(pairs)


__all__ = [
    "BASELINE_VERSION",
    "LAYERS_VERSION",
    "LayerConfigService",
    "read_baseline",
    "write_baseline",
]
