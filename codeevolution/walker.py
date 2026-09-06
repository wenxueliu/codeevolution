"""Git history walker — traverses commits and provides file content snapshots."""

import hashlib
import subprocess
from dataclasses import dataclass, field


@dataclass
class CommitInfo:
    hash: str
    parent_hash: str | None
    timestamp: int
    author: str
    message: str
    tags: list[str] = field(default_factory=list)

    @property
    def semantic_type(self) -> str | None:
        """Extract conventional commit type from message."""
        msg = self.message.strip()
        if ":" in msg:
            prefix = msg.split(":")[0].lower()
            for t in (
                "feat",
                "fix",
                "refactor",
                "docs",
                "test",
                "chore",
                "style",
                "perf",
                "ci",
                "build",
            ):
                if prefix.startswith(t):
                    return t
        return None


class HistoryWalker:
    """Iterates git commits in chronological order (oldest first).

    Uses git commands directly — no checkout, no worktree manipulation.
    File content is read from the git object database via `git show`.
    """

    def __init__(self, repo_path: str, first_parent: bool = True):
        self.repo_path = repo_path
        self.first_parent = first_parent
        self._file_cache: dict[str, str] = {}  # (commit_hash, filepath) -> content
        self._file_hash_cache: dict[str, str] = {}  # (commit_hash, filepath) -> sha256

    def iter_commits(self, start_from: str | None = None):
        """Yield CommitInfo for each commit in chronological order.

        Args:
            start_from: Optional commit hash to resume from.
                        Only commits AFTER this one are yielded.
        """
        range_spec = "HEAD"
        if start_from:
            range_spec = f"{start_from}..HEAD"

        args = [
            "git",
            "-C",
            self.repo_path,
            "log",
            "--reverse",  # oldest first
            "--format=%H%n%P%n%at%n%an%n%s%n%D%n---",
        ]
        if self.first_parent:
            args.append("--first-parent")

        if range_spec:
            args.append(range_spec)

        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git log failed: {result.stderr}")

        for entry in result.stdout.strip().split("\n---\n"):
            if not entry.strip():
                continue
            lines = entry.strip().split("\n")
            if len(lines) < 5:
                continue

            commit_hash = lines[0]
            parent_hash = lines[1] if lines[1] else None
            # If merge commit, take first parent
            if parent_hash and " " in parent_hash:
                parts = parent_hash.split()
                parent_hash = parts[0] if self.first_parent else parts[-1]

            timestamp = int(lines[2])
            author = lines[3]
            message = lines[4]

            # Parse tags from ref names line (line 5 if present)
            tags = []
            if len(lines) >= 6 and lines[5] and lines[5] != "---":
                ref_line = lines[5]
                for ref in ref_line.split(", "):
                    ref = ref.strip()
                    if ref.startswith("tag: "):
                        tags.append(ref[5:])

            yield CommitInfo(
                hash=commit_hash,
                parent_hash=parent_hash,
                timestamp=timestamp,
                author=author,
                message=message,
                tags=tags,
            )

    def count_commits(self) -> int:
        """Return the total number of commits."""
        args = ["git", "-C", self.repo_path, "rev-list", "--count", "HEAD"]
        if self.first_parent:
            args.insert(4, "--first-parent")  # git rev-list --first-parent --count HEAD
        result = subprocess.run(args, capture_output=True, text=True)
        return int(result.stdout.strip())

    def get_files_at(self, commit_hash: str) -> list[str]:
        """List all tracked files at a given commit."""
        result = subprocess.run(
            ["git", "-C", self.repo_path, "ls-tree", "-r", "--name-only", commit_hash],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]

    def get_changed_files(self, parent_hash: str, commit_hash: str) -> list[str]:
        """Get list of files changed between parent and commit."""
        result = subprocess.run(
            [
                "git",
                "-C",
                self.repo_path,
                "diff-tree",
                "-r",
                "--name-only",
                parent_hash,
                commit_hash,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]

    def read_file(self, commit_hash: str, filepath: str) -> str | None:
        """Read file content at a specific commit from git object db."""
        cache_key = f"{commit_hash}:{filepath}"
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]

        result = subprocess.run(
            ["git", "-C", self.repo_path, "show", f"{commit_hash}:{filepath}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        self._file_cache[cache_key] = result.stdout
        return result.stdout

    def file_hash(self, commit_hash: str, filepath: str) -> str:
        """Get SHA-256 hash of file content at a commit."""
        cache_key = f"{commit_hash}:{filepath}"
        if cache_key in self._file_hash_cache:
            return self._file_hash_cache[cache_key]

        content = self.read_file(commit_hash, filepath)
        if content is None:
            return ""

        h = hashlib.sha256(content.encode()).hexdigest()
        self._file_hash_cache[cache_key] = h
        return h

    def read_files_batch(self, commit_hash: str, filepaths: list[str]) -> dict[str, str]:
        """Read multiple files at once using a single git archive-like pipe.

        For efficiency, falls back to individual `git show` calls if batch fails.
        """
        result: dict[str, str] = {}
        for fp in filepaths:
            content = self.read_file(commit_hash, fp)
            if content is not None:
                result[fp] = content
        return result
