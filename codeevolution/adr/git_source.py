"""Git inputs for ADR generation: one commit, or a filtered commit history."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIFF_CHARS = 60_000
MAX_COMMIT_SUBJECT = 4_000

_NOISE_PATTERNS = (
    re.compile(r"^docs?/", re.IGNORECASE),
    re.compile(r"\.(md|txt|rst)$", re.IGNORECASE),
    re.compile(r"(^|/)(tests?|__tests__|specs?)/", re.IGNORECASE),
    re.compile(r"(^|/)(test_[^/]+|.*\.(spec|test)\.[a-z]+)$", re.IGNORECASE),
    re.compile(r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|go\.sum)$", re.IGNORECASE),
    re.compile(r"\.(lock)$", re.IGNORECASE),
    re.compile(r"(^|/)(\.gitignore|LICENSE|CHANGELOG.*)$", re.IGNORECASE),
)


class AdrGitError(RuntimeError):
    """The repository or git ref could not be read."""


@dataclass
class CommitContext:
    sha: str
    message: str
    author: str
    committed_at: str
    files: list[str] = field(default_factory=list)
    diff: str = ""
    parent_count: int = 0
    diff_truncated: bool = False

    @property
    def is_merge(self) -> bool:
        return self.parent_count > 1


def _run(repo: str | Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AdrGitError((result.stderr or "").strip() or f"git {' '.join(args)} failed")
    return result.stdout


def significant_files(files: list[str]) -> bool:
    """True when at least one changed file can carry an architecture decision."""
    return any(not any(pattern.search(path) for pattern in _NOISE_PATTERNS) for path in files)


def resolve_commit(repo: str | Path, ref: str) -> str:
    output = _run(repo, "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}").strip()
    if not output:
        raise AdrGitError(f"cannot resolve commit {ref!r}")
    return output.splitlines()[0]


def read_commit(
    repo: str | Path,
    ref: str = "HEAD",
    *,
    max_diff_chars: int = MAX_DIFF_CHARS,
) -> CommitContext:
    sha = resolve_commit(repo, ref)
    meta = _run(repo, "show", "-s", "--format=%an%n%aI%n%P%n%s", sha).rstrip("\n")
    lines = meta.split("\n")
    author = lines[0] if lines else ""
    committed_at = lines[1] if len(lines) > 1 else ""
    parents = lines[2].split() if len(lines) > 2 and lines[2].strip() else []
    subject = "\n".join(lines[3:]).strip() if len(lines) > 3 else ""
    body = _run(repo, "show", "-s", "--format=%b", sha).strip()
    message = subject if not body else f"{subject}\n\n{body}"
    message = message[:MAX_COMMIT_SUBJECT]

    files = [
        line.strip()
        for line in _run(repo, "show", "--no-color", "--format=", "--name-only", sha).splitlines()
        if line.strip()
    ]
    patch = _run(repo, "show", "--no-color", "--format=", "--unified=3", "--patch", sha)
    truncated = len(patch) > max_diff_chars
    if truncated:
        patch = patch[:max_diff_chars] + "\n... [diff truncated]\n"
    return CommitContext(
        sha=sha,
        message=message,
        author=author,
        committed_at=committed_at,
        files=files,
        diff=patch,
        parent_count=len(parents),
        diff_truncated=truncated,
    )


def list_commits(
    repo: str | Path,
    *,
    since: str = "",
    until: str = "",
    limit: int = 20,
    include_merges: bool = False,
) -> list[dict]:
    """Newest-first commit candidates with their changed files."""
    limit = max(1, min(int(limit), 100))
    args = ["log", "-n", str(limit), "--format=%H"]
    if not include_merges:
        args.append("--no-merges")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args.append(until or "HEAD")
    shas = [line.strip() for line in _run(repo, *args).splitlines() if line.strip()]
    commits: list[dict] = []
    for sha in shas:
        meta = _run(repo, "show", "-s", "--format=%an%n%aI%n%s", sha).rstrip("\n").split("\n")
        files = [
            line.strip()
            for line in _run(repo, "show", "--no-color", "--format=", "--name-only", sha).splitlines()
            if line.strip()
        ]
        commits.append(
            {
                "sha": sha,
                "author": meta[0] if meta else "",
                "committed_at": meta[1] if len(meta) > 1 else "",
                "subject": meta[2] if len(meta) > 2 else "",
                "files": files,
                "significant": significant_files(files),
            }
        )
    return commits


__all__ = [
    "AdrGitError",
    "CommitContext",
    "list_commits",
    "read_commit",
    "resolve_commit",
    "significant_files",
]
