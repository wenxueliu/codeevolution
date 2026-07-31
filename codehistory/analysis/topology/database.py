"""Static database-access collector for service-to-database flow edges."""

import re
from pathlib import Path

from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

DB_CALL_PATTERNS = (
    "execute",
    "executemany",
    "query",
    "select",
    "insert",
    "update",
    "delete",
    "save",
    "find",
    "findone",
    "findall",
    "repository",
    "cursor",
)


class DatabaseAccessCollector:
    def __init__(self, repos: list[dict]):
        self.repos = repos

    def collect(self) -> dict[str, list[dict]]:
        result = {}
        for repo in self.repos:
            database = Path(repo["path"]) / ".codegraph" / "codegraph.db"
            if not database.exists():
                result[repo["name"]] = []
                continue
            with SQLiteCodeGraphRepository(str(database)) as repository:
                rows = repository.database_call_candidates()
            accesses = []
            for row in rows:
                searchable = " ".join(
                    str(row.get(key) or "") for key in ("target", "name", "signature", "metadata")
                )
                if not any(pattern in searchable.lower() for pattern in DB_CALL_PATTERNS):
                    continue
                accesses.append(
                    {
                        "function": row["function"],
                        "target": row["target"],
                        "table": self.extract_table(searchable),
                        "evidence": {"caller": row["function"], "callee": row["target"]},
                    }
                )
            result[repo["name"]] = accesses
        return result

    @staticmethod
    def extract_table(text: str) -> str:
        match = re.search(r"\b(?:from|into|update|join|table)\s+([\w.\"`]+)", text, re.I)
        return match.group(1).strip('"`') if match else "unknown"
