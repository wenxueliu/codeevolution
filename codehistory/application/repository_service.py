"""Repository registry use cases."""

from __future__ import annotations


class RepositoryService:
    def __init__(self, repository):
        self.repository = repository

    def list(self) -> list[dict]:
        return self.repository.load()

    def save(self, entries: list[dict]) -> None:
        self.repository.save(entries)
