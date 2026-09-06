"""Configuration management for CodeEvolution."""

from dataclasses import dataclass
from pathlib import Path

from .paths import repo_data_file


@dataclass
class Config:
    """CodeEvolution configuration.

    CodeEvolution uses CodeGraph for parsing and call resolution.
    Run ``codegraph init`` on the target repo before running backfill.

    All paths are resolved relative to repo_path.
    """

    repo_path: str
    db_path: str = ""

    # Analysis scope
    first_parent: bool = True

    # Matcher
    l1_match_threshold: float = 0.9
    l2_match_threshold: float = 0.6

    # Analyzer
    growth_threshold_ratio: float = 1.3
    shrink_threshold_ratio: float = 0.7

    def __post_init__(self):
        repo = Path(self.repo_path).resolve()
        if not repo.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")
        if not (repo / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_path}")

        self.repo_path = str(repo)

        if not self.db_path:
            database = repo_data_file(repo, "evolution.db")
            database.parent.mkdir(exist_ok=True)
            self.db_path = str(database)

    @property
    def codegraph_db_path(self) -> str:
        """Path to CodeGraph's SQLite database for this repo."""
        return str(Path(self.repo_path) / ".codegraph" / "codegraph.db")
