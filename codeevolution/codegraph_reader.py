"""Compatibility facade for the CodeGraph SQLite repository."""

from .domain.knowledge import CallTarget, EntryPointDef, FunctionDef  # legacy re-exports
from .infrastructure.codegraph_sqlite import (
    HTTP_DECORATORS,
    HTTP_DIR_PATTERNS,
    TEST_PATTERNS,
    SQLiteCodeGraphRepository,
)


class CodeGraphReader:
    """Legacy API that delegates every operation to the infrastructure adapter."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._repository = SQLiteCodeGraphRepository(db_path)

    def close(self):
        return self._repository.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._repository.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._repository, name)


__all__ = [
    "CallTarget",
    "CodeGraphReader",
    "EntryPointDef",
    "FunctionDef",
    "HTTP_DECORATORS",
    "HTTP_DIR_PATTERNS",
    "TEST_PATTERNS",
]
