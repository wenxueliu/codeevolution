from dataclasses import dataclass

import pytest

from codehistory.analysis.refactoring import TECHNIQUE_BY_ID, TECHNIQUES
from codehistory.application.refactoring_service import RefactoringPlanningService
from codehistory.domain.knowledge import CallTarget, FunctionDef
from codehistory.infrastructure.refactoring_techniques import RefactoringTechniqueCatalog


def function(node_id, name, path, is_test=False, line=1):
    return FunctionDef(
        node_id=node_id,
        name=name,
        qualified_name=f"{path}::{name}",
        file_path=path,
        language="python",
        start_line=line,
        end_line=line + 10,
        kind="function",
        is_test=is_test,
    )


@dataclass
class GraphStub:
    include_direct_test: bool = False

    def __post_init__(self):
        self.target = function("target", "calculate_total", "src/order.py", line=20)
        self.caller = function("caller", "submit_order", "src/api.py", line=10)
        self.dependency = function("tax", "calculate_tax", "src/tax.py")
        self.test = function("test", "test_calculate_total", "tests/test_order.py", True)

    def get_all_functions(self):
        return [self.target, self.caller, self.dependency, self.test]

    def get_callers(self, node_id):
        if node_id != "target":
            return []
        return [CallTarget("caller", "target", "submit_order", "function", "src/api.py", 10, 12)]

    def get_callees(self, node_id):
        if node_id == "target":
            return [CallTarget("target", "tax", "calculate_tax", "function", "src/tax.py", 1, 25)]
        if node_id == "test":
            covered = "target" if self.include_direct_test else "caller"
            return [CallTarget("test", covered, "covered", "function", "src/order.py", 20, 5)]
        return []


def test_catalog_contains_24_independently_selectable_techniques():
    assert len(TECHNIQUES) == 24
    assert len(TECHNIQUE_BY_ID) == 24
    assert "extract-method" in TECHNIQUE_BY_ID


def test_missing_direct_test_generates_test_task_and_blocks_refactoring(monkeypatch):
    service = RefactoringPlanningService(".", GraphStub(include_direct_test=False))
    monkeypatch.setattr(
        service,
        "_git_file_stats",
        lambda *_: {"src/order.py": {"commits": {"abc", "def"}, "authors": {"A"}, "changed_lines": 40}},
    )

    plan = service.plan("extract-method", limit=1)[0].to_dict()

    assert plan["test_gate"]["status"] == "partial"
    assert plan["test_gate"]["refactoring_allowed"] is False
    assert plan["agent_task"]["task_type"] == "add_characterization_tests"
    assert plan["agent_task"]["constraints"]["production_changes_allowed"] is False


def test_direct_test_allows_one_small_refactoring_task(monkeypatch):
    service = RefactoringPlanningService(".", GraphStub(include_direct_test=True))
    monkeypatch.setattr(
        service,
        "_git_file_stats",
        lambda *_: {"src/order.py": {"commits": {"abc"}, "authors": {"A"}, "changed_lines": 20}},
    )

    plan = service.plan("decompose-conditional", limit=1)[0].to_dict()

    assert plan["test_gate"]["status"] == "sufficient"
    assert plan["agent_task"]["task_type"] == "refactor"
    assert plan["agent_task"]["constraints"]["single_technique"] == "decompose-conditional"
    assert plan["agent_task"]["constraints"]["max_changed_lines"] == 100


def test_window_must_be_incremental_and_non_overlapping():
    service = RefactoringPlanningService(".", GraphStub())
    with pytest.raises(ValueError, match="smaller"):
        service.plan("extract-method", window_days=7, previous_window_days=7)


def test_technique_catalog_adds_custom_and_overrides_builtin_atomically(tmp_path):
    catalog = RefactoringTechniqueCatalog(tmp_path / "techniques.json")
    created = catalog.create(
        {"id": "extract-domain-service", "name": "提取领域服务", "objective": "集中领域行为", "checks": ["逻辑散落"]}
    )
    assert created["source"] == "custom"
    assert catalog.get("extract-domain-service").name == "提取领域服务"

    updated = catalog.update(
        "extract-method",
        {"id": "extract-method", "name": "提取小函数", "objective": "降低单函数职责", "checks": ["长函数"]},
    )
    assert updated["source"] == "customized"
    assert catalog.get("extract-method").name == "提取小函数"
    assert len(catalog.list()) == 25


def test_planner_uses_a_custom_technique_catalog(monkeypatch, tmp_path):
    catalog = RefactoringTechniqueCatalog(tmp_path / "techniques.json")
    catalog.create({"id": "domain-check", "name": "领域检查", "objective": "收拢领域逻辑", "checks": ["逻辑散落"]})
    service = RefactoringPlanningService(".", GraphStub(include_direct_test=True), catalog)
    monkeypatch.setattr(service, "_git_file_stats", lambda *_: {"src/order.py": {"commits": {"abc"}, "authors": {"A"}, "changed_lines": 20}})

    plan = service.plan("domain-check", limit=1)[0]

    assert plan.technique["id"] == "domain-check"
    assert plan.agent_task["constraints"]["single_technique"] == "domain-check"


def test_each_repository_has_an_independent_technique_catalog(tmp_path):
    first = RefactoringTechniqueCatalog(tmp_path / "repo-a" / ".codehistory" / "refactoring-techniques.json")
    second = RefactoringTechniqueCatalog(tmp_path / "repo-b" / ".codehistory" / "refactoring-techniques.json")
    first.update("extract-method", {"id": "extract-method", "name": "仓库 A 提取", "objective": "A 目标", "checks": ["A 检查"]})
    second.update("extract-method", {"id": "extract-method", "name": "仓库 B 提取", "objective": "B 目标", "checks": ["B 检查"]})

    assert first.get("extract-method").name == "仓库 A 提取"
    assert second.get("extract-method").name == "仓库 B 提取"


def test_deleting_customization_removes_custom_or_restores_builtin(tmp_path):
    catalog = RefactoringTechniqueCatalog(tmp_path / "techniques.json")
    catalog.create({"id": "domain-check", "name": "领域检查", "objective": "检查领域逻辑", "checks": ["逻辑散落"]})
    catalog.update("extract-method", {"id": "extract-method", "name": "定制提取", "objective": "仓库目标", "checks": ["仓库检查"]})

    assert catalog.delete("domain-check") == {"id": "domain-check", "deleted": True, "restored_builtin": False}
    assert catalog.get("domain-check") is None
    assert catalog.delete("extract-method")["restored_builtin"] is True
    assert catalog.get("extract-method").name == "提取函数"
