"""Configuration management for CodeHistory."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """CodeHistory configuration.

    All paths are resolved relative to repo_path.
    """

    repo_path: str
    db_path: str = ""

    # Analysis scope
    first_parent: bool = True
    cluster_window_minutes: int = 0  # 0 = disabled

    # Parser
    languages: list[str] = field(default_factory=lambda: ["python", "java", "javascript", "typescript", "tsx", "vue"])

    # Matcher
    l1_match_threshold: float = 0.9
    l2_match_threshold: float = 0.6

    # Analyzer
    growth_threshold_ratio: float = 1.3  # call_tree_nodes 增长 > 1.3x → GROWN 事件
    shrink_threshold_ratio: float = 0.7  # call_tree_nodes 收缩 < 0.7x → SHRUNK 事件

    def __post_init__(self):
        repo = Path(self.repo_path).resolve()
        if not repo.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")
        if not (repo / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_path}")

        self.repo_path = str(repo)

        if not self.db_path:
            data_dir = repo / ".codehistory"
            data_dir.mkdir(exist_ok=True)
            self.db_path = str(data_dir / "evolution.db")
