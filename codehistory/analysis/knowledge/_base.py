"""Shared adapter used while preserving the original public extractor."""

from collections.abc import Callable
from typing import Any


class ExtractionStep:
    """One independently testable report dimension."""

    def __init__(self, extract: Callable[[], Any]):
        self._extract = extract

    def extract(self) -> Any:
        return self._extract()

