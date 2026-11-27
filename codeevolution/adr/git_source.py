"""Git inputs for ADR generation: one commit, or a filtered commit history."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..text_encoding import decode_text_lines

MAX_DIFF_CHARS = 60_000
MAX_COMMIT_SUBJECT = 4_000
# One git log record per commit: \x1e starts a record, \x1f separates fields,
# so subjects and file lists never need a separate git process per commit.
# ``%P`` carries the parents: merge commits stay in the table (they are marked
# as skipped, not hidden), so the caller needs to recognise them.
_LOG_FORMAT = "%x1e%H%x1f%an%x1f%aI%x1f%P%x1f%s"

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
    # Decode per line: a commit authored in a cp936 console carries GBK bytes,
    # while the rest of the repository is UTF-8.  A single strict decode would
    # turn the whole patch into replacement characters.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if result.returncode != 0:
        message = decode_text_lines(result.stderr or b"").strip()
        raise AdrGitError(message or f"git {' '.join(args)} failed")
    return decode_text_lines(result.stdout)


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


def head_commit(repo: str | Path) -> str:
    """The commit the history view is anchored on; also the index cache key."""
    try:
        return _run(repo, "rev-parse", "HEAD").strip()
    except AdrGitError:
        return ""


def count_commits(
    repo: str | Path,
    *,
    since: str = "",
    until: str = "",
    include_merges: bool = True,
) -> int:
    """Number of commits a listing would page through, without reading patches.

    Merges are counted by default so the total matches ``git rev-list --count``;
    the table lists them as skipped instead of hiding them.
    """
    args = ["rev-list", "--count"]
    if not include_merges:
        args.append("--no-merges")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    output = _run(repo, *args, "HEAD").strip()
    try:
        return int(output or 0)
    except ValueError:
        return 0


def _parse_log_records(output: str) -> list[dict]:
    commits: list[dict] = []
    for chunk in output.split("\x1e"):
        lines = chunk.strip("\n").split("\n")
        if not lines or not lines[0].strip():
            continue
        header = lines[0].split("\x1f")
        sha = header[0].strip()
        if not sha:
            continue
        parents = header[3].split() if len(header) > 3 and header[3].strip() else []
        files = [line.strip() for line in lines[1:] if line.strip()]
        commits.append(
            {
                "sha": sha,
                "author": header[1] if len(header) > 1 else "",
                "committed_at": header[2] if len(header) > 2 else "",
                "subject": header[4] if len(header) > 4 else "",
                "parents": len(parents),
                "is_merge": len(parents) > 1,
                "files": files,
                "significant": significant_files(files),
            }
        )
    return commits


def list_commits(
    repo: str | Path,
    *,
    since: str = "",
    until: str = "",
    limit: int = 20,
    offset: int = 0,
    include_merges: bool = True,
) -> list[dict]:
    """Newest-first commit candidates with their changed files.

    One ``git log`` call answers a whole page (or the whole history when the
    caller asks for ``count_commits()`` entries), so the web table can page
    through every commit without one process per row.  Merge commits carry
    ``is_merge=True`` and an empty file list; callers mark them as skipped.
    """
    limit = max(1, min(int(limit), 100_000))
    offset = max(0, int(offset))
    args = [
        "log",
        "-n",
        str(limit),
        f"--skip={offset}",
        f"--format={_LOG_FORMAT}",
        "--name-only",
        "--no-color",
    ]
    if not include_merges:
        args.append("--no-merges")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args.append("HEAD")
    return _parse_log_records(_run(repo, *args))


def current_files(repo: str | Path, limit: int = 300) -> list[str]:
    """Tracked files in the working tree, used as backtest evidence context."""
    try:
        output = _run(repo, "ls-files")
    except AdrGitError:
        return []
    files = [line.strip() for line in output.splitlines() if line.strip()]
    return files[: max(1, limit)]


def recent_subjects(repo: str | Path, limit: int = 10) -> list[str]:
    try:
        output = _run(repo, "log", f"-n{max(1, limit)}", "--format=%h %s")
    except AdrGitError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


__all__ = [
    "AdrGitError",
    "CommitContext",
    "count_commits",
    "current_files",
    "head_commit",
    "list_commits",
    "read_commit",
    "recent_subjects",
    "resolve_commit",
    "significant_files",
]
