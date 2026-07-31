#!/usr/bin/env python3
"""Dependency-free line coverage gate for offline development environments."""

import argparse
import dis
import sys
import trace
from pathlib import Path
from types import CodeType

import pytest

COMPATIBILITY_AND_DELIVERY = {
    "api.py",
    "cli.py",
    "codegraph_reader.py",
    "cross_repo.py",
    "knowledge.py",
    "llm.py",
    "mcp_server.py",
    "p2_advanced.py",
    "registry.py",
}


def executable_lines(code: CodeType) -> set[int]:
    lines = {line for _, line in dis.findlinestarts(code) if line is not None}
    for value in code.co_consts:
        if isinstance(value, CodeType):
            lines.update(executable_lines(value))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-under", type=float, help="Legacy alias for --core-fail-under")
    parser.add_argument("--core-fail-under", type=float, default=45.0)
    parser.add_argument("--full-fail-under", type=float, default=35.0)
    parser.add_argument("pytest_args", nargs="*", default=["-q"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_root = root / "codehistory"

    tracer = trace.Trace(count=True, trace=False)
    result = tracer.runfunc(pytest.main, args.pytest_args or ["-q"])
    if result != 0:
        return int(result)

    counts = tracer.results().counts
    hits_by_file: dict[Path, set[int]] = {}
    for filename, line in counts:
        resolved = Path(filename).resolve()
        hits_by_file.setdefault(resolved, set()).add(line)
    core_total = core_covered = full_total = full_covered = 0
    rows = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(root)
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        executable = executable_lines(code)
        hit = hits_by_file.get(path.resolve(), set())
        file_covered = len(executable & hit)
        full_total += len(executable)
        full_covered += file_covered
        is_core = not (path.parent == source_root and path.name in COMPATIBILITY_AND_DELIVERY)
        if is_core:
            core_total += len(executable)
            core_covered += file_covered
        rows.append((str(relative), file_covered, len(executable), is_core))

    for name, file_covered, file_total, is_core in rows:
        file_percentage = 100.0 * file_covered / file_total if file_total else 100.0
        scope = "core" if is_core else "delivery/compat"
        print(f"{name:65} {file_covered:4}/{file_total:<4} {file_percentage:6.2f}% {scope}")
    core_percentage = 100.0 * core_covered / core_total if core_total else 100.0
    full_percentage = 100.0 * full_covered / full_total if full_total else 100.0
    core_gate = args.fail_under if args.fail_under is not None else args.core_fail_under
    print(f"CORE TOTAL {core_covered}/{core_total} {core_percentage:.2f}% (gate {core_gate:.2f}%)")
    print(f"FULL TOTAL {full_covered}/{full_total} {full_percentage:.2f}% (gate {args.full_fail_under:.2f}%)")
    if core_percentage < core_gate or full_percentage < args.full_fail_under:
        print("Coverage gate failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
