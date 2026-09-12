"""Path and environment compatibility helpers for the project rename."""

from __future__ import annotations

import os
from pathlib import Path


def environment_value(name: str, default: str = "") -> str:
    """Read a CodeEvolution setting, falling back to its legacy name."""
    legacy_name = name.replace("CODEEVOLUTION_", "CODEHISTORY_", 1)
    return os.environ.get(name) or os.environ.get(legacy_name, default)


def data_dir() -> Path:
    """Return a writable data directory without orphaning existing data.

    Managed/container runtimes can expose ``HOME`` as read-only. Keep an
    explicit ``CODEEVOLUTION_DATA_DIR`` authoritative, but use the service's
    local data directory as a fallback for the default location so settings
    such as the LLM API key can still be persisted.
    """
    configured = environment_value("CODEEVOLUTION_DATA_DIR")
    if configured:
        return Path(configured)

    current = Path.home() / ".codeevolution"
    legacy = Path.home() / ".codehistory"
    preferred = current if current.exists() or not legacy.exists() else legacy
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-probe"
        probe.touch(exist_ok=False)
        probe.unlink()
        return preferred
    except OSError:
        fallback = Path(__file__).resolve().parents[1] / "data" / ".codeevolution"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def analysis_data_dir() -> Path:
    """Return the snapshot data root without falling back to legacy storage."""
    configured = os.environ.get("CODEEVOLUTION_DATA_DIR")
    if configured:
        return Path(configured)
    current = Path.home() / ".codeevolution"
    # Some managed runtimes expose the home directory read-only after a cleanup.
    # Keep the snapshot store usable in that case without resurrecting legacy data.
    try:
        current.mkdir(parents=True, exist_ok=True)
        probe = current / ".write-probe"
        probe.touch(exist_ok=False)
        probe.unlink()
        return current
    except OSError:
        fallback = Path(__file__).resolve().parents[1] / "data" / ".codeevolution"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def shared_data_file(filename: str) -> Path:
    """Prefer a current global data file, falling back to that exact legacy file."""
    configured = environment_value("CODEEVOLUTION_DATA_DIR")
    if configured:
        return Path(configured) / filename
    current = Path.home() / ".codeevolution" / filename
    legacy = Path.home() / ".codehistory" / filename
    return current if current.exists() or not legacy.exists() else legacy


def repo_data_file(repo: str | Path, filename: str) -> Path:
    """Prefer a renamed repo-local file and fall back to an existing legacy one."""
    repo = Path(repo)
    current = repo / ".codeevolution" / filename
    legacy = repo / ".codehistory" / filename
    return current if current.exists() or not legacy.exists() else legacy
