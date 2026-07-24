"""Multi-repo registry — manages a list of registered repositories."""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class RepoEntry:
    name: str
    path: str
    db_path: str = ""


REGISTRY_DIR = Path.home() / ".codehistory"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"


def ensure_registry():
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_FILE.exists():
        REGISTRY_FILE.write_text("[]")


def load_registry() -> list[dict]:
    ensure_registry()
    try:
        data = json.loads(REGISTRY_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_registry(entries: list[dict]):
    ensure_registry()
    REGISTRY_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def register_repo(name: str, path: str) -> dict:
    """Register a new repo in the registry."""
    abs_path = str(Path(path).resolve())
    if not Path(abs_path, ".git").exists():
        raise ValueError(f"Not a git repository: {abs_path}")

    db_path = str(Path(abs_path) / ".codehistory" / "evolution.db")

    entries = load_registry()

    # Check for duplicate name or path
    for e in entries:
        if e["name"] == name:
            raise ValueError(f"Repo name '{name}' already registered")
        if e["path"] == abs_path:
            raise ValueError(f"Repo path '{abs_path}' already registered as '{e['name']}'")

    entry = {"name": name, "path": abs_path, "db_path": db_path}
    entries.append(entry)
    save_registry(entries)
    return entry


def unregister_repo(name: str):
    entries = load_registry()
    entries = [e for e in entries if e["name"] != name]
    save_registry(entries)


def get_repo(name: str) -> dict | None:
    entries = load_registry()
    for e in entries:
        if e["name"] == name:
            return e
    return None


def list_repos() -> list[dict]:
    return load_registry()
