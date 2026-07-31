"""Knowledge report orchestration independent of storage and delivery."""

from collections.abc import Mapping
from typing import Any

from ._base import ExtractionStep


class KnowledgeReportBuilder:
    def __init__(self, steps: Mapping[str, ExtractionStep]):
        self.steps = dict(steps)

    def build(self) -> dict[str, Any]:
        return {name: step.extract() for name, step in self.steps.items()}

