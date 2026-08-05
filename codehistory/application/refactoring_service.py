"""Incremental refactoring planning over Git history and CodeGraph."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..analysis.refactoring import classify_risk
from ..domain.knowledge import FunctionDef
from ..domain.refactoring import Hotspot, RefactoringPlan
from ..infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
from ..infrastructure.refactoring_techniques import RefactoringTechniqueCatalog


class RefactoringPlanningService:
    """Build small, test-gated Agent tasks for one technique at a time."""

    def __init__(self, repo_path: str, graph: Any, technique_catalog=None):
        self.repo_path = str(Path(repo_path).resolve())
        self.graph = graph
        self.technique_catalog = technique_catalog or RefactoringTechniqueCatalog()

    @classmethod
    def from_repository(cls, repo_path: str) -> "RefactoringPlanningService":
        repo = Path(repo_path).resolve()
        db_path = repo / ".codegraph" / "codegraph.db"
        if not db_path.exists():
            raise ValueError(f"CodeGraph database not found: {db_path}")
        catalog = RefactoringTechniqueCatalog(repo / ".codehistory" / "refactoring-techniques.json")
        return cls(str(repo), SQLiteCodeGraphRepository(str(db_path)), catalog)

    def close(self) -> None:
        close = getattr(self.graph, "close", None)
        if close:
            close()

    def plan(
        self,
        technique_id: str,
        window_days: int = 7,
        previous_window_days: int = 0,
        limit: int = 5,
        min_tests: int = 1,
    ) -> list[RefactoringPlan]:
        technique = self.technique_catalog.get(technique_id)
        if technique is None:
            raise ValueError(f"Unknown refactoring technique: {technique_id}")
        if window_days <= 0 or previous_window_days < 0:
            raise ValueError("Window values must be non-negative and window-days must be positive")
        if previous_window_days >= window_days:
            raise ValueError("previous-window-days must be smaller than window-days")

        file_stats = self._git_file_stats(window_days, previous_window_days)
        hotspots = self._hotspots(file_stats)[: max(limit, 0)]
        return [
            self._build_plan(hotspot, technique, window_days, previous_window_days, min_tests)
            for hotspot in hotspots
        ]

    def _git_file_stats(self, window_days: int, previous_days: int) -> dict[str, dict]:
        command = [
            "git", "log", "--no-merges", f"--since={window_days} days ago",
            "--format=@@%H%x09%an", "--numstat",
        ]
        if previous_days:
            command.insert(4, f"--until={previous_days} days ago")
        result = subprocess.run(
            command,
            cwd=self.repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
        stats: dict[str, dict] = defaultdict(
            lambda: {"commits": set(), "authors": set(), "changed_lines": 0}
        )
        commit = author = ""
        for line in result.stdout.splitlines():
            if line.startswith("@@"):
                commit, _, author = line[2:].partition("\t")
                continue
            parts = line.split("\t")
            if len(parts) != 3 or not commit:
                continue
            added, deleted, file_path = parts
            if added == "-" or deleted == "-":
                changed = 0
            else:
                changed = int(added) + int(deleted)
            item = stats[file_path]
            item["commits"].add(commit)
            item["authors"].add(author)
            item["changed_lines"] += changed
        return dict(stats)

    def _hotspots(self, file_stats: dict[str, dict]) -> list[Hotspot]:
        functions: list[FunctionDef] = self.graph.get_all_functions()
        candidates: list[Hotspot] = []
        for function in functions:
            stats = file_stats.get(function.file_path)
            if not stats or function.is_test:
                continue
            commit_count = len(stats["commits"])
            author_count = len(stats["authors"])
            changed_lines = stats["changed_lines"]
            file_functions = sum(1 for item in functions if item.file_path == function.file_path)
            apportioned_lines = max(1, changed_lines // max(file_functions, 1))
            score = round(commit_count * 4 + author_count * 2 + min(apportioned_lines, 200) / 20, 2)
            candidates.append(
                Hotspot(
                    node_id=function.node_id,
                    name=function.name,
                    qualified_name=function.qualified_name,
                    file_path=function.file_path,
                    start_line=function.start_line,
                    end_line=function.end_line,
                    commit_count=commit_count,
                    author_count=author_count,
                    changed_lines=apportioned_lines,
                    score=score,
                    commits=sorted(stats["commits"]),
                )
            )
        return sorted(candidates, key=lambda item: (-item.score, item.file_path, item.start_line))

    def _build_plan(
        self,
        hotspot: Hotspot,
        technique,
        window_days: int,
        previous_days: int,
        min_tests: int,
    ) -> RefactoringPlan:
        callers = self.graph.get_callers(hotspot.node_id)
        callees = self.graph.get_callees(hotspot.node_id)
        tests = self._related_tests(hotspot.node_id, {caller.caller_node_id for caller in callers})
        direct_tests = [test for test in tests if test["coverage"] == "direct"]
        test_status = "sufficient" if len(direct_tests) >= min_tests else "partial" if tests else "missing"
        impact_count = 1 + len(callers) + len(callees)
        risk = classify_risk(impact_count)
        impact = {
            "direct_callers": [self._call_dict(call) for call in callers],
            "direct_callees": [self._call_dict(call) for call in callees],
            "affected_symbol_count": impact_count,
            "risk": risk,
        }
        test_gate = {
            "status": test_status,
            "required_direct_tests": min_tests,
            "related_tests": tests,
            "refactoring_allowed": test_status == "sufficient" and risk in {"LOW", "MEDIUM"},
            "assessment": "Static CodeGraph call coverage; branch and assertion quality require Agent review.",
        }
        task = (
            self._refactoring_task(hotspot, technique, impact, tests)
            if test_gate["refactoring_allowed"]
            else self._test_task(hotspot, technique, impact, tests, test_status)
        )
        return RefactoringPlan(
            version="1.0",
            repository=self.repo_path,
            time_window={
                "window_days": window_days,
                "previous_window_days": previous_days,
                "mode": "incremental-ring" if previous_days else "initial-window",
            },
            technique={
                "id": technique.id,
                "name": technique.name,
                "objective": technique.objective,
                "checks": list(technique.checks),
            },
            hotspot=hotspot.__dict__,
            codegraph_impact=impact,
            test_gate=test_gate,
            agent_task=task,
        )

    def _related_tests(self, target_id: str, caller_ids: set[str]) -> list[dict]:
        related = []
        for function in self.graph.get_all_functions():
            if not function.is_test:
                continue
            called = {call.callee_node_id for call in self.graph.get_callees(function.node_id)}
            coverage = "direct" if target_id in called else "caller" if called & caller_ids else ""
            if coverage:
                related.append(
                    {
                        "qualified_name": function.qualified_name,
                        "file_path": function.file_path,
                        "line": function.start_line,
                        "coverage": coverage,
                    }
                )
        return related

    @staticmethod
    def _call_dict(call) -> dict:
        return {
            "node_id": call.caller_node_id,
            "target_node_id": call.callee_node_id,
            "name": call.callee_name,
            "file_path": call.callee_file,
            "line": call.callee_line,
            "provenance": call.provenance,
        }

    @staticmethod
    def _test_task(hotspot, technique, impact, tests, status) -> dict:
        return {
            "task_type": "add_characterization_tests",
            "title": f"为 {hotspot.qualified_name} 建立重构安全网",
            "reason": [
                f"热点符号在窗口内涉及 {hotspot.commit_count} 个提交",
                f"准备检查的唯一重构手法是 {technique.name}",
                f"测试门禁状态为 {status}，禁止先修改生产代码",
            ],
            "target": {
                "file": hotspot.file_path,
                "symbol": hotspot.qualified_name,
                "lines": [hotspot.start_line, hotspot.end_line],
            },
            "existing_tests": tests,
            "test_cases": [
                {"name": "锁定正常路径", "then": "记录当前输入、返回值和可观察副作用"},
                {"name": "锁定边界条件", "then": "覆盖空值、零值、集合边界或类型边界中适用的场景"},
                {"name": "锁定失败路径", "then": "保持当前异常类型、错误返回和传播方式"},
                {"name": "锁定协作顺序", "then": "验证关键依赖的参数、调用次数和顺序"},
            ],
            "instructions": [
                "阅读目标实现、直接调用者和直接依赖，删除不适用的通用用例",
                "在项目现有测试目录和框架中生成最小特征测试",
                "只修改测试文件，不修改生产代码或业务预期",
                "先在当前实现上运行新增测试并记录命令与结果",
                "测试 Patch 独立提交；通过后再创建重构 Patch",
            ],
            "constraints": {
                "production_changes_allowed": False,
                "one_task_one_purpose": True,
                "affected_symbol_count": impact["affected_symbol_count"],
            },
            "acceptance": [
                "新增测试在重构前通过",
                "覆盖与目标重构相关的正常、边界、失败和副作用行为",
                "测试不依赖真实外部服务且可重复运行",
            ],
        }

    @staticmethod
    def _refactoring_task(hotspot, technique, impact, tests) -> dict:
        return {
            "task_type": "refactor",
            "title": f"对 {hotspot.qualified_name} 执行一次{technique.name}检查与最小修改",
            "reason": [
                f"热点符号在窗口内涉及 {hotspot.commit_count} 个提交",
                f"本轮只允许使用 {technique.name}",
                "CodeGraph 已找到直接影响范围且测试门禁通过",
            ],
            "target": {
                "file": hotspot.file_path,
                "symbol": hotspot.qualified_name,
                "lines": [hotspot.start_line, hotspot.end_line],
            },
            "instructions": [
                f"只检查 {', '.join(technique.checks)}",
                f"若命中，按“{technique.objective}”实施最小修改；未命中则报告 skipped",
                "按接口、实现、调用者、测试的顺序更新必要代码",
                "运行重构前已通过的同一组测试，并报告命令和结果",
                "重新检查实际 Diff 的 CodeGraph 影响范围",
            ],
            "files_allowed": sorted({hotspot.file_path, *(test["file_path"] for test in tests)}),
            "constraints": {
                "single_technique": technique.id,
                "max_files": 3,
                "max_symbols": 5,
                "max_changed_lines": 100,
                "do_not_change_test_expectations": True,
                "do_not_change_public_behavior": True,
            },
            "validation": {
                "risk": impact["risk"],
                "related_tests": tests,
                "required": ["原有测试通过", "新增特征测试通过", "修改未超出 Patch Budget"],
            },
        }
