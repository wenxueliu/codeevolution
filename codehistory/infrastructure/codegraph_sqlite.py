"""The shared SQLite adapter for CodeGraph databases."""

import sqlite3
from collections.abc import Sequence
from typing import Any

from ..codegraph_reader import CodeGraphReader
from ..domain.knowledge import CallTarget, EntryPointDef, FunctionDef


def read_rows(db_path: str, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Execute a read-only query with deterministic connection cleanup."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, tuple(params or ())).fetchall()
        return [dict(row) for row in rows]


class SQLiteCodeGraphRepository(CodeGraphReader):
    """Typed CodeGraph repository; legacy method names remain inherited."""

    def functions(self) -> list[FunctionDef]:
        return self.get_all_functions()

    def callers(self, node_id: str) -> list[CallTarget]:
        return self.get_callers(node_id)

    def callees(self, node_id: str) -> list[CallTarget]:
        return self.get_callees(node_id)

    def inbound_endpoints(self) -> list[EntryPointDef]:
        return self.get_entry_points()

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(sql, tuple(params or ())).fetchall()
        return [dict(row) for row in rows]
