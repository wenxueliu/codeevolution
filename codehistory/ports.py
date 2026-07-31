"""Dependency-inversion ports shared by analysis and infrastructure."""

from typing import Protocol

from .domain.knowledge import CallTarget, EntryPointDef, FunctionDef


class SourceProvider(Protocol):
    def read_text(self, path: str) -> str | None: ...

    def snippet(self, path: str, start: int, end: int) -> str | None: ...


class CodeGraphRepository(Protocol):
    def functions(self) -> list[FunctionDef]: ...

    def callers(self, node_id: str) -> list[CallTarget]: ...

    def callees(self, node_id: str) -> list[CallTarget]: ...

    def inbound_endpoints(self) -> list[EntryPointDef]: ...
