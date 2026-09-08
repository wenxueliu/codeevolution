"""Lossless, line-addressed source chunking for function explanations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_BOUNDARY = re.compile(
    r"^\s*(?:if\b|elif\b|else\b|for\b|while\b|try\b|except\b|finally\b|"
    r"switch\b|case\b|match\b|catch\b|return\b|raise\b|throw\b)"
)


def source_hash(source: str) -> str:
    """Hash exactly the source text supplied to the analysis snapshot."""

    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceChunk:
    chunk_index: int
    line_start: int
    line_end: int
    source: str
    source_hash: str


@dataclass(frozen=True)
class ChunkingResult:
    chunks: tuple[SourceChunk, ...]
    source_hash: str
    line_start: int
    line_end: int
    expected_line_end: int | None
    source_complete: bool
    truncated: bool
    truncation_reason: str = ""

    @property
    def uncovered_lines(self) -> tuple[int, ...]:
        if self.line_end < self.line_start:
            return ()
        covered: set[int] = set()
        for chunk in self.chunks:
            covered.update(range(chunk.line_start, chunk.line_end + 1))
        return tuple(line for line in range(self.line_start, self.line_end + 1) if line not in covered)


def chunk_source(
    source: str,
    *,
    line_start: int = 1,
    max_lines: int = 120,
    overlap_lines: int = 8,
    expected_line_end: int | None = None,
    source_complete: bool = True,
) -> ChunkingResult:
    """Split source without dropping any supplied line.

    A nearby control-flow line is preferred as the next chunk boundary.  The
    fixed-size fallback is intentionally conservative and chunks overlap to
    retain local context.  If the provider says input is incomplete, or its
    declared end line exceeds the supplied text, truncation is explicit.
    """

    if line_start < 1:
        raise ValueError("line_start must be positive")
    if max_lines < 1:
        raise ValueError("max_lines must be positive")
    if overlap_lines < 0 or overlap_lines >= max_lines:
        raise ValueError("overlap_lines must be >= 0 and less than max_lines")

    lines = source.splitlines(keepends=True)
    supplied_end = line_start + len(lines) - 1
    declared_missing = expected_line_end is not None and supplied_end < expected_line_end
    complete = bool(source_complete and not declared_missing)
    reasons: list[str] = []
    if not source_complete:
        reasons.append("source provider marked input incomplete")
    if declared_missing:
        reasons.append(f"expected through line {expected_line_end}, received through line {supplied_end}")

    chunks: list[SourceChunk] = []
    start_index = 0
    while start_index < len(lines):
        hard_end = min(start_index + max_lines, len(lines))
        end_index = hard_end
        if hard_end < len(lines):
            minimum = start_index + max(1, max_lines // 2)
            candidates = [
                idx for idx in range(minimum, hard_end + 1) if idx < len(lines) and _BOUNDARY.match(lines[idx])
            ]
            if candidates:
                end_index = candidates[-1]
        # Guard against a pathological boundary at the current position.
        end_index = max(start_index + 1, end_index)
        chunk_text = "".join(lines[start_index:end_index])
        absolute_start = line_start + start_index
        absolute_end = line_start + end_index - 1
        chunks.append(
            SourceChunk(
                chunk_index=len(chunks),
                line_start=absolute_start,
                line_end=absolute_end,
                source=chunk_text,
                source_hash=source_hash(chunk_text),
            )
        )
        if end_index == len(lines):
            break
        start_index = end_index - overlap_lines

    return ChunkingResult(
        chunks=tuple(chunks),
        source_hash=source_hash(source),
        line_start=line_start,
        line_end=supplied_end,
        expected_line_end=expected_line_end,
        source_complete=complete,
        truncated=not complete,
        truncation_reason="; ".join(reasons),
    )


class SourceChunker:
    def __init__(self, max_lines: int = 120, overlap_lines: int = 8):
        self.max_lines = max_lines
        self.overlap_lines = overlap_lines

    def chunk(self, source: str, **kwargs) -> ChunkingResult:
        return chunk_source(
            source,
            max_lines=self.max_lines,
            overlap_lines=self.overlap_lines,
            **kwargs,
        )
