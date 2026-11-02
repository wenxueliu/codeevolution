"""Canonical business term types and their legacy on-disk aliases.

New records are written with the canonical values; snapshots created before
the Domain Evidence Graph contract keep their lowercase values and are mapped
on read so no history is rewritten.
"""

from __future__ import annotations

TERM_ENTITY = "ENTITY"
TERM_BUSINESS_EVENT = "BUSINESS_EVENT"
TERM_BUSINESS_ACTION = "BUSINESS_ACTION"
TERM_BUSINESS_ATTRIBUTE = "BUSINESS_ATTRIBUTE"
TERM_BUSINESS_ENUM = "BUSINESS_ENUM"
TERM_COMMAND = "COMMAND"
TERM_API_RESOURCE = "API_RESOURCE"
TERM_DTO = "DTO"
TERM_TECHNICAL_COMPONENT = "TECHNICAL_COMPONENT"
# Auxiliary types beyond the four core business terms.
TERM_PAGE = "PAGE"

TERM_TYPES = (
    TERM_ENTITY,
    TERM_BUSINESS_EVENT,
    TERM_BUSINESS_ACTION,
    TERM_BUSINESS_ATTRIBUTE,
    TERM_BUSINESS_ENUM,
    TERM_COMMAND,
    TERM_API_RESOURCE,
    TERM_DTO,
    TERM_TECHNICAL_COMPONENT,
    TERM_PAGE,
)

_LEGACY = {
    "entity": TERM_ENTITY,
    "value_object": TERM_ENTITY,
    "event": TERM_BUSINESS_EVENT,
    "action": TERM_BUSINESS_ACTION,
    "attribute": TERM_BUSINESS_ATTRIBUTE,
    "state": TERM_BUSINESS_ENUM,
    "enum": TERM_BUSINESS_ENUM,
    "command": TERM_COMMAND,
    "resource": TERM_API_RESOURCE,
    "dto": TERM_DTO,
    "technical": TERM_TECHNICAL_COMPONENT,
    "page": TERM_PAGE,
}
LEGACY_TERM_TYPES = _LEGACY
_ALIASES: dict[str, list[str]] = {}
for _legacy, _canonical in _LEGACY.items():
    _ALIASES.setdefault(_canonical, []).append(_legacy)


def canonical_term_type(value: str) -> str:
    value = str(value or "").strip()
    if value in TERM_TYPES:
        return value
    return _LEGACY.get(value.lower(), value.upper())


def term_type_aliases(value: str) -> set[str]:
    """Values that may appear on disk for a canonical term type."""
    canonical = canonical_term_type(value)
    return {canonical, canonical.lower(), *_ALIASES.get(canonical, [])}
