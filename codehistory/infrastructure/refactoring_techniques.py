"""Persistent customizations for the refactoring technique catalog."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..analysis.refactoring import TECHNIQUES
from ..domain.refactoring import RefactoringTechnique
from .registry_json import RegistryRepository

TECHNIQUE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class RefactoringTechniqueCatalog:
    """Merge immutable built-ins with editable user definitions."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            data_dir = Path(
                os.environ.get("CODEHISTORY_DATA_DIR", str(Path.home() / ".codehistory"))
            )
            path = data_dir / "refactoring-techniques.json"
        self.repository = RegistryRepository(path)

    def list(self) -> list[dict]:
        builtins = {item.id: self._serialize(item, source="builtin") for item in TECHNIQUES}
        for saved in self.repository.load():
            try:
                normalized = self._normalize(saved)
            except ValueError:
                continue
            technique_id = normalized["id"]
            normalized["source"] = "customized" if technique_id in builtins else "custom"
            builtins[technique_id] = normalized
        return sorted(builtins.values(), key=lambda item: (item["source"] == "custom", item["name"]))

    def get(self, technique_id: str) -> RefactoringTechnique | None:
        item = next((item for item in self.list() if item["id"] == technique_id), None)
        if item is None:
            return None
        return RefactoringTechnique(item["id"], item["name"], item["objective"], tuple(item["checks"]))

    def create(self, value: dict) -> dict:
        item = self._normalize(value)
        if any(existing["id"] == item["id"] for existing in self.list()):
            raise ValueError(f"Refactoring technique '{item['id']}' already exists")
        saved = self.repository.load()
        saved.append(item)
        self.repository.save(saved)
        return {**item, "source": "custom"}

    def update(self, technique_id: str, value: dict) -> dict:
        if value.get("id", technique_id) != technique_id:
            raise ValueError("Technique id cannot be changed")
        if not any(existing["id"] == technique_id for existing in self.list()):
            raise ValueError(f"Refactoring technique '{technique_id}' not found")
        item = self._normalize({**value, "id": technique_id})
        saved = [entry for entry in self.repository.load() if entry.get("id") != technique_id]
        saved.append(item)
        self.repository.save(saved)
        builtin_ids = {technique.id for technique in TECHNIQUES}
        return {**item, "source": "customized" if technique_id in builtin_ids else "custom"}

    def delete(self, technique_id: str) -> dict:
        saved = self.repository.load()
        remaining = [entry for entry in saved if entry.get("id") != technique_id]
        if len(remaining) == len(saved):
            raise ValueError(f"Refactoring technique customization '{technique_id}' not found")
        if remaining:
            self.repository.save(remaining)
        else:
            self.repository.path.unlink(missing_ok=True)
        builtin_ids = {technique.id for technique in TECHNIQUES}
        return {
            "id": technique_id,
            "deleted": True,
            "restored_builtin": technique_id in builtin_ids,
        }

    @staticmethod
    def _serialize(technique: RefactoringTechnique, source: str) -> dict:
        return {
            "id": technique.id,
            "name": technique.name,
            "objective": technique.objective,
            "checks": list(technique.checks),
            "source": source,
        }

    @staticmethod
    def _normalize(value: dict) -> dict:
        technique_id = str(value.get("id", "")).strip()
        name = str(value.get("name", "")).strip()
        objective = str(value.get("objective", "")).strip()
        raw_checks = value.get("checks", [])
        checks = [str(item).strip() for item in raw_checks if str(item).strip()] if isinstance(raw_checks, list) else []
        if not TECHNIQUE_ID_PATTERN.fullmatch(technique_id):
            raise ValueError("Technique id must use lower-case kebab-case")
        if not name or not objective:
            raise ValueError("Technique name and objective are required")
        if not checks:
            raise ValueError("At least one inspection check is required")
        if len(name) > 100 or len(objective) > 500 or any(len(check) > 200 for check in checks):
            raise ValueError("Technique definition is too long")
        return {"id": technique_id, "name": name, "objective": objective, "checks": checks}
