"""Architecture Decision Record (ADR) extraction for one repository checkout.

Decision records are repository data, like the layer declaration: they live in
the checkout (``docs/adr`` by default), not in CodeGraph.  This module is the
CodeEvolution port of the ``adrkit`` model and R1/R2 checks shipped by the
``sw-adr-engineering`` skill, trimmed to the read-only knowledge dimension:

* MADR 4.0 frontmatter (``status``/``date``/``decision-makers``/``supersedes``)
  with a legacy ``## Status`` fallback, so adr-tools repositories work as-is;
* R1 structure checks (filename, duplicate numbers, title, status, date,
  placeholders, required MADR sections, numbering gaps);
* R2 supersession checks (target exists, bidirectional link, cycles).

It stays dependency-free: PyYAML is used when installed, otherwise a small
stdlib parser covers the flat frontmatter MADR emits.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any

try:  # optional: keep the dimension usable without any installed dependency
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised when PyYAML is absent
    _yaml = None

ADR_VERSION = "adr/v1"
ADR_DIR_CANDIDATES = ("docs/adr", "doc/adr", "docs/decisions", "adr")
STATUS_VALUES = ("proposed", "accepted", "rejected", "deprecated")

FILENAME_RE = re.compile(r"^(?P<number>\d{4})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
SUPERSEDED_BY_RE = re.compile(r"^superseded\s+by\s+ADR-(\d{4})$", re.IGNORECASE)
REQUIRED_SECTIONS = ("Context and Problem Statement", "Decision Outcome")
PLACEHOLDER_MARKERS = (
    "<!-- short title",
    "<!-- decision driver",
    "<!-- option",
    "<!-- title of option",
    'chosen option: ""',
    "<!-- describe the context",
)
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", "template", "templates"}
SKIP_NAMES = {"index.md", "readme.md"}
INDEX_HEADER = "# Architecture Decision Records"
_MAX_RECORDS = 200
_MAX_VIOLATIONS = 200

_SUPERSEDES_BODY_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]*)?supersedes?[ \t]*:?[ \t]*(?:ADR[-\s]?)?(\d{4})\b"
)
_SOURCE_COMMIT_BODY_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]*)?(?:source-commits?|source|commit)"
    r"[ \t]*:?[ \t]*([0-9a-f]{7,40})\b"
)


@dataclasses.dataclass
class Adr:
    """One parsed ADR file."""

    path: Path
    relpath: str
    number: int | None
    slug: str
    title: str
    status: str
    date: str
    frontmatter: dict[str, Any]
    body: str
    supersedes: list[int]
    source_commits: list[str]
    filename_error: str | None = None

    @property
    def superseded_by(self) -> int | None:
        match = SUPERSEDED_BY_RE.match(self.status)
        return int(match.group(1)) if match else None

    @property
    def is_madr(self) -> bool:
        return "## Context and Problem Statement" in self.body


@dataclasses.dataclass(frozen=True)
class AdrIssue:
    rule: str
    level: str  # "error" | "warning"
    path: str
    message: str


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.lower() in {"null", "~", "none"}:
        return None
    return value


def _minimal_yaml(text: str) -> dict[str, Any]:
    """Parse the flat YAML subset MADR frontmatter uses."""
    data: dict[str, Any] = {}
    key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in {" ", "\t"}:
            if key is not None and stripped.startswith("- "):
                current = data.get(key)
                if not isinstance(current, list):
                    current = []
                    data[key] = current
                current.append(_scalar(stripped[2:]))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if not value:
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [_scalar(item) for item in value[1:-1].split(",") if item.strip()]
        else:
            data[key] = _scalar(value)
    return data


def _parse_yaml(text: str) -> dict[str, Any]:
    if _yaml is not None:
        try:
            parsed = _yaml.safe_load(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return _minimal_yaml(text)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = _normalize_text(text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return _parse_yaml(raw), body
    return {}, text


def _section_value(body: str, heading: str) -> str:
    """First meaningful line of a ``## Heading`` section (legacy status)."""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", body, re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    rest = body[match.end() :]
    next_heading = re.search(r"^##\s", rest, re.MULTILINE)
    block = rest[: next_heading.start()] if next_heading else rest
    for line in block.splitlines():
        value = line.strip().strip("*` ")
        if value and not value.startswith("<!--"):
            return value
    return ""


def _normalize_status(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if value is None:
        return ""
    status = str(value).strip().strip('"').strip("'").strip()
    if not status:
        return ""
    match = SUPERSEDED_BY_RE.match(status)
    if match:
        return f"superseded by ADR-{int(match.group(1)):04d}"
    lowered = status.lower()
    if lowered in STATUS_VALUES:
        return lowered
    if lowered in {"superseded", "superseded-by"}:
        return "superseded"
    if lowered in {"draft", "pending"}:
        return "proposed"
    return status


def _iso_date(value: Any) -> str:
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text or text.startswith("{"):
        return ""
    return text


def _extract_title(body: str) -> str:
    """First level-1 heading, without an ``ADR-0001:``-style prefix."""
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if not match:
            continue
        raw = re.sub(r"<!--.*?-->", "", match.group(1)).strip()
        raw = re.sub(r"^(?:ADR[-\s]?)?\d{3,4}\s*[:.\-\u2013\u2014]\s*", "", raw)
        return raw.strip()
    return ""


def _extract_supersedes(frontmatter: dict[str, Any], body: str) -> list[int]:
    numbers: list[int] = []
    for key in ("supersedes", "superseded"):
        value = frontmatter.get(key)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            match = re.search(r"(?:ADR[-\s]?)?(\d{4})\b", str(item), re.IGNORECASE)
            if match:
                numbers.append(int(match.group(1)))
    for match in _SUPERSEDES_BODY_RE.finditer(body):
        numbers.append(int(match.group(1)))
    return sorted(set(numbers))


def _extract_source_commits(frontmatter: dict[str, Any], body: str) -> list[str]:
    found: list[str] = []
    for key in ("source-commit", "source-commits", "source_commit"):
        value = frontmatter.get(key)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            for match in re.findall(r"\b([0-9a-f]{7,40})\b", str(item), re.IGNORECASE):
                found.append(match.lower())
    for match in _SOURCE_COMMIT_BODY_RE.finditer(body):
        found.append(match.group(1).lower())
    return list(dict.fromkeys(found))


def _extract_people(frontmatter: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        value = frontmatter.get(key)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        people = [str(item).strip() for item in values if str(item).strip()]
        if people:
            return people
    return []


def parse_adr(path: Path, adr_dir: Path) -> Adr:
    """Parse one ADR file.  Unreadable files surface as empty records."""
    relpath = path.relative_to(adr_dir).as_posix()
    match = FILENAME_RE.match(path.name)
    number = int(match.group("number")) if match else None
    slug = match.group("slug") if match else path.stem
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        text = ""
    frontmatter, body = _split_frontmatter(text)
    status = _normalize_status(frontmatter.get("status"))
    if not status:
        status = _normalize_status(_section_value(body, "Status"))
    return Adr(
        path=path,
        relpath=relpath,
        number=number,
        slug=slug,
        title=_extract_title(body),
        status=status,
        date=_iso_date(frontmatter.get("date")),
        frontmatter=frontmatter,
        body=body,
        supersedes=_extract_supersedes(frontmatter, body),
        source_commits=_extract_source_commits(frontmatter, body),
        filename_error=None if match else "filename must match NNNN-kebab-slug.md (lowercase ASCII)",
    )


def iter_adr_files(adr_dir: Path, recursive: bool = True) -> list[Path]:
    """ADR markdown files, excluding indexes, READMEs and templates."""
    if not adr_dir.is_dir():
        return []
    pattern = "**/*.md" if recursive else "*.md"
    files: list[Path] = []
    for path in adr_dir.glob(pattern):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(adr_dir).parts[:-1]
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative_parts):
            continue
        if path.name.lower() in SKIP_NAMES:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(adr_dir).as_posix())


def load_adrs(adr_dir: Path, recursive: bool = True) -> list[Adr]:
    adrs = [parse_adr(path, adr_dir) for path in iter_adr_files(adr_dir, recursive)]
    adrs.sort(
        key=lambda item: (
            item.number is None,
            item.number if item.number is not None else 0,
            item.relpath,
        )
    )
    return adrs


def render_index(adrs: list[Adr]) -> str:
    """Deterministic ``index.md`` content, so staleness is a byte compare."""
    valid = sorted((adr for adr in adrs if adr.number is not None), key=lambda adr: adr.number)
    lines = [INDEX_HEADER, ""]
    for adr in valid:
        title = adr.title or "(untitled)"
        annotation = "" if adr.status == "accepted" else f" ({adr.status or 'unknown status'})"
        lines.append(f"* [ADR-{adr.number:04d}]({adr.relpath}) - {title}{annotation}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _section_text(body: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", body, re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    rest = body[match.end() :]
    next_heading = re.search(r"^#{2,3}\s", rest, re.MULTILINE)
    block = rest[: next_heading.start()] if next_heading else rest
    return re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL).strip()


def _numbering_ranges(numbers: list[int]) -> str:
    if not numbers:
        return ""
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [numbers[-1] + 2]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"{start:04d}" if start == previous else f"{start:04d}-{previous:04d}")
        start = previous = number
    return ", ".join(f"ADR-{item}" for item in ranges)


def _valid_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_adrs(adrs: list[Adr], adr_dir_label: str) -> list[AdrIssue]:
    """Return every R1/R2 issue, sorted by path then rule."""
    issues: list[AdrIssue] = []
    by_number: dict[int, Adr] = {}

    for adr in adrs:
        if adr.filename_error:
            issues.append(AdrIssue("R1.1", "error", adr.relpath, adr.filename_error))
            continue

        assert adr.number is not None
        if adr.number in by_number:
            issues.append(
                AdrIssue(
                    "R1.2",
                    "error",
                    adr.relpath,
                    f"duplicate ADR number {adr.number:04d} "
                    f"(also used by {by_number[adr.number].relpath})",
                )
            )
        else:
            by_number[adr.number] = adr

        if not adr.title:
            issues.append(
                AdrIssue("R1.4", "error", adr.relpath, "missing title (expected '# <title>')")
            )

        if not adr.status:
            issues.append(
                AdrIssue(
                    "R1.5",
                    "error",
                    adr.relpath,
                    "missing status (frontmatter 'status:' or '## Status' section)",
                )
            )
        elif adr.status == "superseded":
            issues.append(
                AdrIssue(
                    "R1.6",
                    "error",
                    adr.relpath,
                    "status 'superseded' must name its replacement: 'superseded by ADR-NNNN'",
                )
            )
        elif adr.status not in STATUS_VALUES and adr.superseded_by is None:
            issues.append(
                AdrIssue(
                    "R1.6",
                    "error",
                    adr.relpath,
                    f"invalid status {adr.status!r}; expected one of {', '.join(STATUS_VALUES)}, "
                    "or 'superseded by ADR-NNNN'",
                )
            )

        if adr.date and not _valid_date(adr.date):
            issues.append(
                AdrIssue("R1.7", "warning", adr.relpath, f"date {adr.date!r} is not YYYY-MM-DD")
            )

        lowered = adr.body.lower()
        markers = [marker for marker in PLACEHOLDER_MARKERS if marker in lowered]
        if markers:
            issues.append(
                AdrIssue(
                    "R1.8",
                    "error",
                    adr.relpath,
                    "unfilled template placeholder(s): " + ", ".join(repr(marker) for marker in markers),
                )
            )

        for heading in REQUIRED_SECTIONS:
            if not _section_text(adr.body, heading):
                issues.append(
                    AdrIssue(
                        "R1.9",
                        "warning",
                        adr.relpath,
                        f"MADR section missing or empty: '## {heading}'",
                    )
                )

    if not adrs:
        issues.append(AdrIssue("R1.0", "warning", adr_dir_label, "no ADR files found"))
    elif by_number:
        highest = max(by_number)
        missing = sorted(set(range(1, highest + 1)) - set(by_number))
        if missing:
            issues.append(
                AdrIssue("R1.3", "warning", adr_dir_label, f"numbering gap(s): {_numbering_ranges(missing)}")
            )

    # ---- R2: supersession references -----------------------------------
    for adr in by_number.values():
        if adr.superseded_by is None:
            continue
        target_number = adr.superseded_by
        if target_number == adr.number:
            issues.append(
                AdrIssue(
                    "R2.3",
                    "error",
                    adr.relpath,
                    f"ADR-{adr.number:04d} cannot be superseded by itself",
                )
            )
            continue
        target = by_number.get(target_number)
        if target is None:
            issues.append(
                AdrIssue(
                    "R2.1",
                    "error",
                    adr.relpath,
                    f"status names ADR-{target_number:04d}, which does not exist",
                )
            )
            continue
        if adr.number not in target.supersedes:
            issues.append(
                AdrIssue(
                    "R2.2",
                    "error",
                    adr.relpath,
                    f"ADR-{target_number:04d} does not link back; add "
                    f"'supersedes: ADR-{adr.number:04d}' to its frontmatter",
                )
            )
        if target.status != "accepted":
            issues.append(
                AdrIssue(
                    "R2.5",
                    "warning",
                    adr.relpath,
                    f"replacement ADR-{target_number:04d} is {target.status or 'status-less'}, not accepted",
                )
            )

    for adr in by_number.values():
        for superseded in adr.supersedes:
            old = by_number.get(superseded)
            if old is not None and old.superseded_by != adr.number:
                issues.append(
                    AdrIssue(
                        "R2.6",
                        "warning",
                        adr.relpath,
                        f"claims to supersede ADR-{superseded:04d}, but that ADR's status does not point back",
                    )
                )

    # ---- R2.4: cycles ---------------------------------------------------
    edges = {adr.number: adr.superseded_by for adr in by_number.values() if adr.superseded_by is not None}
    reported: set[frozenset[int]] = set()
    for start in sorted(edges):
        chain: list[int] = []
        current: int | None = start
        while current is not None and current in edges and current not in chain:
            chain.append(current)
            current = edges.get(current)
        if current is not None and current in chain:
            cycle = chain[chain.index(current) :]
            key = frozenset(cycle)
            if key in reported:
                continue
            reported.add(key)
            path = " -> ".join(f"ADR-{number:04d}" for number in cycle + [cycle[0]])
            issues.append(
                AdrIssue("R2.4", "error", by_number[cycle[0]].relpath, f"supersession cycle: {path}")
            )

    issues.sort(key=lambda issue: (issue.path, issue.rule, issue.message))
    return issues


def _locate_adr_dir(root: Path) -> Path | None:
    for candidate in ADR_DIR_CANDIDATES:
        directory = root / candidate
        if directory.is_dir():
            return directory
    return None


def _record(adr: Adr, repo_root: Path) -> dict[str, Any]:
    try:
        repo_path = adr.path.relative_to(repo_root).as_posix()
    except ValueError:  # pragma: no cover - adr_dir always lives under root
        repo_path = adr.relpath
    return {
        "id": f"ADR-{adr.number:04d}" if adr.number is not None else adr.slug,
        "number": adr.number,
        "slug": adr.slug,
        "title": adr.title,
        "status": adr.status,
        "date": adr.date,
        "path": repo_path,
        "supersedes": adr.supersedes,
        "superseded_by": adr.superseded_by,
        "decision_makers": _extract_people(
            adr.frontmatter, "decision-makers", "decision_makers", "decision makers"
        ),
        "source_commits": adr.source_commits,
        "madr": adr.is_madr,
    }


def empty_adr_report(reason: str = "") -> dict[str, Any]:
    """A stable shape for snapshots that carry no ADR facts."""
    return {
        "version": ADR_VERSION,
        "available": False,
        "reason": reason,
        "adr_dir": "",
        "total": 0,
        "by_status": {},
        "index_present": False,
        "index_stale": False,
        "records": [],
        "violations": [],
        "error_count": 0,
        "warning_count": 0,
    }


def adr_content_digest(repo_root: str | Path) -> str:
    """Stable digest of the repository's ADR files.

    ADR Markdown is not part of the frozen source manifest, so its content has
    to join the snapshot identity explicitly; otherwise editing a decision
    record would reuse a snapshot whose facts describe the previous revision.
    """
    root = Path(repo_root).resolve()
    try:
        adr_dir = _locate_adr_dir(root)
    except OSError:
        return ""
    if adr_dir is None:
        return ""
    digest = hashlib.sha256()
    for path in iter_adr_files(adr_dir):
        try:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except (OSError, ValueError):
            continue
    return digest.hexdigest()


class AdrExtractor:
    """Read MADR/legacy decision records from one repository checkout."""

    def __init__(self, repo_root: str | Path):
        self.root = Path(repo_root).resolve()

    def extract(self) -> dict[str, Any]:
        try:
            adr_dir = _locate_adr_dir(self.root)
        except OSError:
            return empty_adr_report("adr directory is unreadable")
        if adr_dir is None:
            return empty_adr_report("no ADR directory found")
        try:
            adrs = load_adrs(adr_dir)
        except OSError:
            return empty_adr_report("adr directory is unreadable")

        label = adr_dir.relative_to(self.root).as_posix()
        issues = check_adrs(adrs, label)
        index_path = adr_dir / "index.md"
        index_present = index_path.is_file()
        index_stale = False
        if index_present:
            try:
                current = _normalize_text(index_path.read_text(encoding="utf-8-sig"))
                index_stale = current != render_index(adrs)
            except (OSError, UnicodeError):
                index_stale = True

        records = [_record(adr, self.root) for adr in adrs]
        return {
            "version": ADR_VERSION,
            "available": True,
            "reason": "",
            "adr_dir": label,
            "total": len(records),
            "by_status": _count_statuses(records),
            "index_present": index_present,
            "index_stale": index_stale,
            "records": records[:_MAX_RECORDS],
            "violations": [
                dataclasses.asdict(issue) for issue in issues[:_MAX_VIOLATIONS]
            ],
            "error_count": sum(1 for issue in issues if issue.level == "error"),
            "warning_count": sum(1 for issue in issues if issue.level == "warning"),
        }


def _count_statuses(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = record.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = [
    "ADR_DIR_CANDIDATES",
    "ADR_VERSION",
    "AdrExtractor",
    "AdrIssue",
    "adr_content_digest",
    "empty_adr_report",
]
