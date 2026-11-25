"""ADR generation subsystem."""

from .domain import (
    DEFAULT_PROMPTS,
    HISTORY_KIND,
    PROMPT_SOURCE_KINDS,
    SOURCE_KINDS,
    default_prompt,
    normalize_decision,
    render_madr,
    render_prompt,
)
from .service import AdrScheduler, AdrService, AdrServiceError
from .store import AdrStore

__all__ = [
    "DEFAULT_PROMPTS",
    "HISTORY_KIND",
    "PROMPT_SOURCE_KINDS",
    "SOURCE_KINDS",
    "AdrScheduler",
    "AdrService",
    "AdrServiceError",
    "AdrStore",
    "default_prompt",
    "normalize_decision",
    "render_madr",
    "render_prompt",
]
