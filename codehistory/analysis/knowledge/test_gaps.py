"""Test-gap analysis independent from the legacy facade."""

from collections.abc import Callable
from typing import Protocol

from ...domain.knowledge import FunctionDef, TestGap

TEST_PATH_PATTERNS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/", "/fixtures/",
    "/e2e/", "/integration/", "/__mocks__/", ".test.", ".spec.", "_test.py",
    "_test.java", "test_", "_test.go", "Test.java", "Tests.java", "Test.kt", "Tests.kt",
)


class _Source(Protocol):
    def get_all_functions(self) -> list[FunctionDef]: ...
    def get_callees(self, node_id: str): ...


class TestGapExtractor:
    def __init__(self, source: _Source | Callable[[], list[TestGap]]):
        self._source = source

    def extract(self) -> list[TestGap]:
        if callable(self._source) and not hasattr(self._source, "get_all_functions"):
            return self._source()
        test_functions, production_functions, covered = self._coverage_data()
        del test_functions
        return [
            TestGap(
                node_id=function.node_id,
                name=function.name,
                qualified_name=function.qualified_name,
                file_path=function.file_path,
                kind=function.kind,
                line=function.start_line,
                is_exported=function.is_exported,
            )
            for function in production_functions
            if function.node_id not in covered
        ]

    def coverage_stats(self) -> dict:
        tests, production, covered = self._coverage_data()
        covered_count = sum(function.node_id in covered for function in production)
        return {
            "test_functions": len(tests),
            "production_functions": len(production),
            "covered_functions": covered_count,
            "coverage_pct": round(100 * covered_count / max(len(production), 1), 1),
            "gap_count": len(production) - covered_count,
        }

    def _coverage_data(self) -> tuple[list[FunctionDef], list[FunctionDef], set[str]]:
        functions = self._source.get_all_functions()  # type: ignore[union-attr]
        tests = [function for function in functions if self.is_test_function(function)]
        production = [function for function in functions if not self.is_test_function(function)]
        covered = {
            callee.callee_node_id
            for test in tests
            for callee in self._source.get_callees(test.node_id)  # type: ignore[union-attr]
        }
        return tests, production, covered

    @staticmethod
    def is_test_function(function: FunctionDef) -> bool:
        return function.is_test or any(
            pattern in function.file_path.lower() for pattern in TEST_PATH_PATTERNS
        )
