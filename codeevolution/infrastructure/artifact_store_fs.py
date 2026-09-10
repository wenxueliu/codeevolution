"""Filesystem content-addressed artifact storage.

Artifacts are assembled below ``staging`` and atomically published into the
``artifacts/sha256`` tree.  Callers remain responsible for committing catalog
metadata only after :meth:`publish` returns.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import time
import uuid
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact cannot be safely opened or published."""


class FileSystemArtifactStore:
    """A same-filesystem CAS with private permissions and atomic publication."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.artifacts_dir = self.data_dir / "artifacts" / "sha256"
        self.staging_dir = self.data_dir / "staging"
        self.trash_dir = self.data_dir / "trash"
        for directory in (self.data_dir, self.artifacts_dir, self.staging_dir, self.trash_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

    def create_staging(self, attempt_id: str) -> Path:
        """Create an empty private staging directory for one attempt."""
        self._validate_component(attempt_id, "attempt id")
        path = self.staging_dir / attempt_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ArtifactStoreError(f"staging already exists: {attempt_id}") from exc
        return path

    def publish(self, staging: str | Path, digest: str) -> str:
        """Publish ``staging`` under ``digest`` and return its canonical key.

        Existing targets are reused only when their deterministic directory
        digest matches.  This makes retries idempotent without trusting names.
        """
        hex_digest = self._parse_digest(digest)
        source = Path(staging).resolve()
        if not self._is_child(source, self.staging_dir) or not source.is_dir():
            raise ArtifactStoreError("publish source is not a staging directory")
        actual = directory_digest(source)
        if actual != hex_digest:
            raise ArtifactStoreError(f"artifact digest mismatch: expected {hex_digest}, got {actual}")

        self._make_tree_private(source)
        self._fsync_tree(source)
        prefix = self.artifacts_dir / hex_digest[:2]
        prefix.mkdir(mode=0o700, exist_ok=True)
        prefix.chmod(0o700)
        target = prefix / hex_digest
        try:
            os.rename(source, target)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if target.is_symlink() or not target.is_dir() or directory_digest(target) != hex_digest:
                raise ArtifactStoreError(f"conflicting artifact target: sha256:{hex_digest}")
            shutil.rmtree(source)
        self._fsync_dir(prefix)
        return f"sha256:{hex_digest}"

    def open(self, artifact_key: str) -> Path:
        """Resolve a key to an existing artifact directory."""
        digest = self._parse_digest(artifact_key)
        path = self.artifacts_dir / digest[:2] / digest
        if not path.is_dir() or path.is_symlink():
            raise ArtifactStoreError(f"artifact unavailable: sha256:{digest}")
        return path

    def move_to_trash(self, artifact_key: str, deletion_id: str | None = None) -> Path:
        """Atomically detach an artifact into recoverable trash."""
        source = self.open(artifact_key)
        token = deletion_id or uuid.uuid4().hex
        self._validate_component(token, "deletion id")
        bucket = self.trash_dir / token
        bucket.mkdir(mode=0o700)
        target = bucket / source.name
        os.rename(source, target)
        self._fsync_dir(source.parent)
        self._fsync_dir(bucket)
        return target

    def purge_trash(self, deletion_id: str) -> None:
        """Permanently remove one previously isolated trash bucket."""
        self._validate_component(deletion_id, "deletion id")
        bucket = (self.trash_dir / deletion_id).resolve()
        if not self._is_child(bucket, self.trash_dir) or not bucket.is_dir() or bucket.is_symlink():
            raise ArtifactStoreError("trash bucket unavailable")
        shutil.rmtree(bucket)
        self._fsync_dir(self.trash_dir)

    def scavenge_staging(
        self, *, max_age_seconds: int = 3600, protected_names: set[str] | frozenset[str] = frozenset()
    ) -> list[str]:
        """Remove abandoned staging directories older than the safety window.

        Active analysis attempts retain their staging directory while they are
        running.  The caller supplies those attempt IDs so an administrative
        scavenger cannot race a worker and remove its input tree.
        """
        now = time.time()
        removed: list[str] = []
        for path in self.staging_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if path.name in protected_names:
                continue
            try:
                age = now - path.stat().st_mtime
                if age >= max_age_seconds:
                    shutil.rmtree(path)
                    removed.append(path.name)
            except FileNotFoundError:
                continue
        if removed:
            self._fsync_dir(self.staging_dir)
        return removed

    def scavenge_trash(self, *, max_age_seconds: int = 86400) -> list[str]:
        """Purge trash buckets that have exceeded the recovery grace period."""
        now = time.time()
        removed: list[str] = []
        for path in self.trash_dir.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                if now - path.stat().st_mtime >= max_age_seconds:
                    shutil.rmtree(path)
                    removed.append(path.name)
            except FileNotFoundError:
                continue
        if removed:
            self._fsync_dir(self.trash_dir)
        return removed

    @staticmethod
    def _validate_component(value: str, label: str) -> None:
        if not value or not _ID_RE.fullmatch(value) or value in {".", ".."}:
            raise ArtifactStoreError(f"invalid {label}")

    @staticmethod
    def _parse_digest(value: str) -> str:
        match = _DIGEST_RE.fullmatch(value)
        if not match:
            raise ArtifactStoreError("artifact key must be a sha256 digest")
        return match.group(1)

    @staticmethod
    def _is_child(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _fsync_tree(cls, root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            cls._fsync_dir(path)
        cls._fsync_dir(root)

    @staticmethod
    def _make_tree_private(root: Path) -> None:
        root.chmod(0o700)
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ArtifactStoreError("artifact staging must not contain symlinks")
            path.chmod(0o700 if path.is_dir() else 0o600)


def directory_digest(root: str | Path) -> str:
    """Hash a directory using relative POSIX paths and raw file bytes."""
    base = Path(root)
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        if path.is_symlink():
            raise ArtifactStoreError("artifact trees cannot contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
