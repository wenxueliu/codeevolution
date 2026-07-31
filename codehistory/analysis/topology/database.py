"""Static database-access collector for service-to-database flow edges."""

import re
from pathlib import Path

from ...infrastructure.codegraph_sqlite import read_rows

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
            rows = read_rows(
                str(database),
                """SELECT DISTINCT caller.qualified_name AS function,
                target.qualified_name AS target, target.name, target.signature, e.metadata
                FROM edges e JOIN nodes caller ON caller.id=e.source
                JOIN nodes target ON target.id=e.target WHERE e.kind='calls'""",
            )
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
                    }
                )
            result[repo["name"]] = accesses
        return result

    @staticmethod
    def extract_table(text: str) -> str:
        match = re.search(r"\b(?:from|into|update|join|table)\s+([\w.\"`]+)", text, re.I)
        return match.group(1).strip('"`') if match else "unknown"
