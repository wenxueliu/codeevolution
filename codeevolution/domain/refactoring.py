"""Domain models for incremental, test-gated refactoring plans."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RefactoringTechnique:
    id: str
    name: str
    objective: str
    checks: tuple[str, ...]


@dataclass
class Hotspot:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    commit_count: int
    author_count: int
    changed_lines: int
    score: float
    commits: list[str] = field(default_factory=list)


@dataclass
class RefactoringPlan:
    version: str
    repository: str
    time_window: dict[str, Any]
    technique: dict[str, Any]
    hotspot: dict[str, Any]
    codegraph_impact: dict[str, Any]
    test_gate: dict[str, Any]
    agent_task: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
