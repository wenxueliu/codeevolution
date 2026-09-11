"""Safe, Git-aware discovery and hashing of repository analysis inputs."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from ..platform import (
    ensure_supported_storage_path,
    is_reparse_point,
    manifest_collision_key,
    normalize_manifest_path,
    open_beneath,
)

INDEXABLE_EXTENSIONS = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".css", ".ex", ".exs", ".go", ".h", ".hpp",
        ".html", ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".php", ".py",
        ".rb", ".rs", ".scala", ".sh", ".sql", ".svelte", ".swift", ".ts", ".tsx",
        ".vue", ".xml",
    }
)
DEFAULT_DECLARED_GLOBS = (
    "**/package.json", "**/pyproject.toml", "**/requirements*.txt", "**/pom.xml",
    "**/build.gradle", "**/build.gradle.kts", "**/Dockerfile", "**/docker-compose*.yml",
    "**/docker-compose*.yaml", "**/*.proto", "**/*.graphql", "**/*.yaml", "**/*.yml",
    "**/*.toml",
)
SECRET_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials"})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer")


@dataclass(frozen=True)
class ScanPolicy:
    indexable_extensions: frozenset[str] = INDEXABLE_EXTENSIONS
    declared_globs: tuple[str, ...] = DEFAULT_DECLARED_GLOBS
    max_file_bytes: int = 10 * 1024 * 1024
    policy_version: str = "workspace-input-v1"


@dataclass(frozen=True)
class InputEntry:
    path: str
    sha256: str
    size: int
    categories: tuple[str, ...]


@dataclass(frozen=True)
class InputExclusion:
    path: str
    reason: str


@dataclass(frozen=True)
class InputObservation:
    entries: tuple[InputEntry, ...]
    exclusions: tuple[InputExclusion, ...]
    source_digest: str
    git_head: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    policy_version: str = "workspace-input-v1"


class WorkspaceScanError(RuntimeError):
    pass


class WorkspaceInputScanner:
    def __init__(self, repo_root: str | Path):
        ensure_supported_storage_path(repo_root)
        candidate = Path(repo_root).expanduser()
        if os.name == "nt" and is_reparse_point(candidate):
            raise WorkspaceScanError("repository root is a Windows reparse point")
        self.root = candidate.resolve()

    def scan(self, policy: ScanPolicy | None = None) -> InputObservation:
        policy = policy or ScanPolicy()
        if not self.root.is_dir():
            raise WorkspaceScanError(f"repository does not exist: {self.root}")

        is_git = (self.root / ".git").exists()
        candidates = self._git_candidates() if is_git else self._walk_candidates()
        declared = self._declared_paths(candidates, policy.declared_globs)
        paths = sorted(candidates)
        entries: list[InputEntry] = []
        exclusions: list[InputExclusion] = []
        collision_keys: set[str] = set()
        for relative in paths:
            collision = manifest_collision_key(relative) if os.name == "nt" else relative
            if collision in collision_keys:
                exclusions.append(InputExclusion(relative, "canonical_path_collision"))
                continue
            collision_keys.add(collision)
            exclusion = self._exclusion(relative, policy)
            if exclusion:
                exclusions.append(InputExclusion(relative, exclusion))
                continue
            category: set[str] = set()
            suffix = PurePosixPath(relative).suffix.lower()
            if suffix in policy.indexable_extensions:
                category.add("indexed")
            if relative in declared:
                category.add("declared")
            if not category:
                continue
            try:
                raw, size = self._read_stable_regular_file(relative, policy.max_file_bytes)
            except WorkspaceScanError as exc:
                exclusions.append(InputExclusion(relative, str(exc)))
                continue
            entries.append(
                InputEntry(
                    path=relative,
                    sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
                    size=size,
                    categories=tuple(sorted(category)),
                )
            )

        entries.sort(key=lambda item: item.path)
        exclusions.sort(key=lambda item: (item.path, item.reason))
        source_digest = _entries_digest(entries)
        head, branch, dirty = self._git_provenance()
        return InputObservation(
            entries=tuple(entries), exclusions=tuple(exclusions), source_digest=source_digest,
            git_head=head, git_branch=branch, git_dirty=dirty, policy_version=policy.policy_version,
        )

    def _git_candidates(self) -> set[str]:
        command = [
            "git", "-C", str(self.root), "ls-files", "-z", "--cached", "--others",
            "--exclude-standard",
        ]
        try:
            result = subprocess.run(command, capture_output=True, check=False)
        except OSError as error:
            raise WorkspaceScanError("git input enumeration failed") from error
        if result.returncode != 0:
            raise WorkspaceScanError("git input enumeration failed")
        return {self._normalize_path(os.fsdecode(raw)) for raw in result.stdout.split(b"\0") if raw}

    def _walk_candidates(self) -> set[str]:
        result: set[str] = set()
        for directory, names, files in os.walk(self.root, followlinks=False):
            base = Path(directory)
            kept: list[str] = []
            for name in sorted(names):
                candidate = base / name
                if name in {".git", ".codegraph", ".codeevolution", "node_modules"}:
                    continue
                if is_reparse_point(candidate):
                    continue
                kept.append(name)
            names[:] = kept
            for name in files:
                result.add(self._normalize_path((base / name).relative_to(self.root).as_posix()))
        return result

    @staticmethod
    def _declared_paths(paths: Iterable[str], globs: Iterable[str]) -> set[str]:
        """Classify already-enumerated paths; never traverse ignored directories."""
        patterns = tuple(globs)
        return {
            path
            for path in paths
            if any(
                fnmatch.fnmatchcase(path, pattern)
                or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
                for pattern in patterns
            )
        }

    @staticmethod
    def _normalize_path(path: str) -> str:
        try:
            normalized = normalize_manifest_path(unicodedata.normalize("NFC", path))
        except ValueError:
            raise WorkspaceScanError("unsafe repository path")
        return normalized

    def _exclusion(self, relative: str, policy: ScanPolicy) -> str | None:
        pure = PurePosixPath(relative)
        lower_name = pure.name.lower()
        if any(part in {".git", ".codegraph", ".codeevolution"} for part in pure.parts):
            return "internal_path"
        if lower_name == ".env" or lower_name.startswith(".env."):
            return "secret_file"
        if lower_name in SECRET_NAMES or lower_name.endswith(SECRET_SUFFIXES):
            return "secret_file"
        candidate = self.root / relative
        if candidate.is_symlink():
            try:
                candidate.resolve(strict=True).relative_to(self.root)
            except (OSError, ValueError):
                return "unsafe_symlink"
            return "symlink"
        if _has_reparse_component(candidate, self.root):
            return "unsafe_reparse_point"
        try:
            stat_result = candidate.stat()
        except OSError:
            return "unreadable"
        if not candidate.is_file():
            return "not_regular_file"
        if stat_result.st_size > policy.max_file_bytes:
            return "file_too_large"
        return None

    def _read_stable_regular_file(self, relative: str, limit: int) -> tuple[bytes, int]:
        path = self.root / relative
        if os.name == "nt" and _has_reparse_component(path, self.root):
            raise WorkspaceScanError("unsafe_or_unreadable")
        try:
            fd = open_beneath(self.root, PurePosixPath(relative))
        except OSError as exc:
            raise WorkspaceScanError("unsafe_or_unreadable") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise WorkspaceScanError("not_regular_file")
            if before.st_size > limit:
                raise WorkspaceScanError("file_too_large")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise WorkspaceScanError("file_too_large")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise WorkspaceScanError("file_changed_during_scan")
            raw = b"".join(chunks)
            if b"\x00" in raw[:8192]:
                raise WorkspaceScanError("binary_file")
            return raw, total
        finally:
            os.close(fd)

    def _git_provenance(self) -> tuple[str | None, str | None, bool | None]:
        if not (self.root / ".git").exists():
            return None, None, None

        def output(*args: str) -> str | None:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "i18n.logOutputEncoding=UTF-8",
                    "-C",
                    str(self.root),
                    *args,
                ],
                capture_output=True,
                check=False,
            )
            return (
                result.stdout.decode("utf-8", errors="replace").strip()
                if result.returncode == 0
                else None
            )

        head = output("rev-parse", "HEAD")
        branch = output("symbolic-ref", "--short", "-q", "HEAD")
        status = output("status", "--porcelain=v1", "--untracked-files=normal")
        return head or None, branch or None, None if status is None else bool(status)


def _entries_digest(entries: Iterable[InputEntry]) -> str:
    import json

    payload = [
        {"category": list(item.categories), "path": item.path, "sha256": item.sha256, "size": item.size}
        for item in sorted(entries, key=lambda item: item.path)
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _has_reparse_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if is_reparse_point(current):
            return True
    return False
