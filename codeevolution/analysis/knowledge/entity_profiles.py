"""Language-specific evidence rules for domain-entity candidates.

The judgement of what counts as a business entity stays human: the profiles
below only decide where each ecosystem usually puts entities and which type
shapes are implementation details (DTOs, schemas, props, generated code).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class EntityProfile:
    """Declarative scoring rules for one language ecosystem."""

    key: str
    label: str
    extensions: tuple[str, ...]
    # (path fragment, delta) pairs; the first match wins inside a category.
    path_bonuses: tuple[tuple[str, float], ...] = ()
    path_penalties: tuple[tuple[str, float], ...] = ()
    annotation_bonuses: tuple[tuple[str, float], ...] = ()
    name_suffix_penalties: tuple[tuple[str, float], ...] = ()
    kind_penalties: tuple[tuple[str, float], ...] = ()
    # Files that never carry business entities for this ecosystem; skipped
    # entirely instead of being ranked at the bottom.
    skip_paths: tuple[str, ...] = ()


JAVA = EntityProfile(
    key="java",
    label="Java / Spring",
    extensions=(".java",),
    path_bonuses=(
        ("/domain/", 8.0), ("/model/", 8.0), ("/entity/", 8.0),
        ("/pojo/", 8.0), ("/types/", 4.0),
    ),
    path_penalties=(
        ("/target/", 10.0), ("/generated/", 8.0), ("/controller/", 6.0),
        ("/service/", 6.0), ("/config/", 6.0), ("/dto/", 6.0),
        ("/vo/", 6.0), ("/request/", 4.0), ("/response/", 4.0),
    ),
    annotation_bonuses=(
        ("entity", 10.0), ("document", 10.0), ("table", 10.0),
        ("aggregate", 10.0),
    ),
    name_suffix_penalties=(
        ("controller", 8.0), ("service", 8.0), ("repository", 8.0),
        ("config", 8.0), ("test", 8.0), ("example", 6.0), ("mapper", 6.0),
        ("dto", 6.0), ("vo", 6.0), ("param", 6.0), ("request", 6.0),
        ("response", 6.0),
    ),
    kind_penalties=(("interface", 2.0),),
    skip_paths=("/target/", "/generated/"),
)

PYTHON = EntityProfile(
    key="python",
    label="Python",
    extensions=(".py",),
    path_bonuses=(
        ("/domain/", 8.0), ("/models/", 8.0), ("/model/", 8.0),
        ("/entities/", 8.0), ("/entity/", 8.0), ("/db/models/", 8.0),
    ),
    path_penalties=(
        ("/tests/", 10.0), ("/test/", 10.0), ("/migrations/", 8.0),
        ("/alembic/", 8.0), ("/schemas/", 6.0), ("/schema/", 6.0),
        ("/serializers/", 6.0), ("/dto/", 6.0), ("/routers/", 6.0),
        ("/api/", 4.0), ("/routes/", 4.0), ("/views/", 4.0),
        ("/scripts/", 4.0), ("/settings", 6.0), ("/config", 6.0),
    ),
    annotation_bonuses=(
        ("declarative", 6.0), ("orm", 4.0), ("dataclass", 3.0),
    ),
    skip_paths=("/migrations/", "/alembic/", "/tests/", "/test/"),
    name_suffix_penalties=(
        ("dispatcher", 8.0), ("handler", 8.0), ("middleware", 8.0),
        ("controller", 8.0), ("worker", 6.0), ("client", 6.0),
        ("service", 6.0), ("manager", 6.0), ("provider", 6.0),
        ("adapter", 6.0), ("factory", 6.0), ("protocol", 6.0),
        ("repository", 6.0), ("store", 6.0), ("schema", 8.0), ("serializer", 8.0), ("request", 8.0),
        ("response", 8.0), ("settings", 8.0), ("config", 8.0),
        ("test", 8.0), ("router", 8.0), ("view", 8.0), ("form", 6.0),
        ("error", 6.0), ("exception", 6.0), ("unavailable", 6.0),
        ("invalid", 6.0), ("conflict", 6.0), ("failure", 6.0),
        ("violation", 6.0), ("mixin", 6.0), ("base", 4.0),
    ),
)

TYPESCRIPT = EntityProfile(
    key="typescript",
    label="TypeScript / JavaScript",
    extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"),
    path_bonuses=(
        (".entity.ts", 12.0), (".entity.", 10.0), (".model.", 8.0),
        ("/entities/", 8.0), ("/entity/", 8.0), ("/domain/", 8.0),
        ("/prisma/", 8.0), ("/models/", 6.0), ("/model/", 6.0),
        ("/schema/", 4.0),
    ),
    path_penalties=(
        (".d.ts", 14.0), ("/node_modules/", 14.0), ("/components/", 10.0),
        ("/pages/", 10.0), ("/styles/", 10.0), ("/__tests__/", 10.0),
        ("/tests/", 10.0), ("/test/", 10.0), ("/generated/", 8.0),
        ("/views/", 8.0), ("/hooks/", 8.0), ("/dto/", 6.0),
        ("/types/", 6.0), ("/store/", 6.0), ("/api/", 4.0),
        ("/schemas/", 4.0),
    ),
    annotation_bonuses=(("entity", 10.0), ("table", 6.0)),
    name_suffix_penalties=(
        ("dto", 8.0), ("props", 8.0), ("request", 8.0), ("response", 8.0),
        ("params", 6.0), ("config", 8.0), ("context", 6.0), ("hook", 8.0),
        ("component", 8.0), ("page", 8.0), ("store", 6.0), ("test", 8.0),
        ("spec", 8.0), ("state", 6.0),
    ),
    kind_penalties=(("interface", 6.0), ("type", 6.0), ("enum", 2.0)),
    skip_paths=(".d.ts", "/node_modules/", "/generated/"),
)

DEFAULT = EntityProfile(
    key="default",
    label="Generic",
    extensions=(),
    path_bonuses=(("/domain/", 8.0), ("/model/", 8.0), ("/entity/", 8.0)),
    path_penalties=(
        ("/controller/", 6.0), ("/service/", 6.0), ("/config/", 6.0),
        ("/generated/", 8.0),
    ),
    annotation_bonuses=(
        ("entity", 10.0), ("document", 10.0), ("table", 10.0),
        ("aggregate", 10.0),
    ),
    name_suffix_penalties=(
        ("controller", 8.0), ("service", 8.0), ("repository", 8.0),
        ("config", 8.0), ("test", 8.0),
    ),
)

PROFILES: tuple[EntityProfile, ...] = (JAVA, PYTHON, TYPESCRIPT, DEFAULT)
_BY_KEY = {profile.key: profile for profile in PROFILES}
_BY_EXTENSION = {
    extension: profile
    for profile in PROFILES
    for extension in profile.extensions
}


def profile_for_path(file_path: str) -> EntityProfile:
    """Pick the ecosystem profile for one source file."""
    path = (file_path or "").replace("\\", "/").lower()
    suffix = PurePosixPath(path).suffix
    return _BY_EXTENSION.get(suffix, DEFAULT)


def profile_for_key(key: str) -> EntityProfile:
    return _BY_KEY.get(key, DEFAULT)


def skipped(file_path: str, profile: EntityProfile) -> bool:
    """True when a file never holds business entities for its ecosystem."""
    path = (file_path or "").replace("\\", "/").lower()
    return any(token in path for token in profile.skip_paths)


def first_match(path: str, rules: tuple[tuple[str, float], ...]) -> tuple[str, float]:
    for token, delta in rules:
        if token in path:
            return token, delta
    return "", 0.0
