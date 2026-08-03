"""Knowledge extraction use cases."""

from collections import defaultdict


class KnowledgeService:
    def __init__(self, extractor, close=None):
        self.extractor = extractor
        self._close = close

    @classmethod
    def from_codegraph(cls, db_path: str):
        from ..infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
        from ..knowledge import KnowledgeExtractor

        repository = SQLiteCodeGraphRepository(db_path)
        return cls(KnowledgeExtractor(repository), repository.close)

    def report(self, include_llm: bool = False) -> dict:
        return self.extractor.extract_all(include_llm=include_llm)

    def section(self, name: str, **options):
        methods = {
            "api": self.extractor.extract_api_contract,
            "modules": self.extractor.extract_module_topology,
            "entities": self.extractor.extract_core_entities,
            "tests": self.extractor.extract_test_coverage_stats,
            "gaps": self.extractor.extract_test_gaps,
            "layers": self.extractor.extract_layer_violations,
            "config": self.extractor.extract_config_consumption,
            "deps": self.extractor.extract_external_dependencies,
            "auth": self.extractor.extract_authorization_model,
            "heatmap": self.extractor.extract_heat_map,
            "business": self.extractor.extract_business_descriptions,
            "rules": self.extractor.extract_business_rules_llm,
            "errors": self.extractor.extract_error_catalog,
            "states": self.extractor.extract_state_machines,
        }
        return methods[name](**options)

    def __getattr__(self, name):
        """Keep delivery migration source-compatible without exposing construction."""
        return getattr(self.extractor, name)

    def close(self):
        if self._close:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class GroupedKnowledgeService:
    """Combine current knowledge reports from repositories in one logical service."""

    def __init__(self, members: list[tuple[str, KnowledgeService, str]]):
        self.members = members

    def report(self, include_llm: bool = False) -> dict:
        reports = [
            (name, service.report(include_llm=include_llm))
            for name, service, _path in self.members
        ]
        result = merge_knowledge_reports(reports, include_llm=include_llm)
        from ..analysis.knowledge.frontend_calls import (
            attach_frontend_callers,
            extract_frontend_calls,
        )

        frontend_calls = [
            call for _name, _service, path in self.members for call in extract_frontend_calls(path)
        ]
        attach_frontend_callers(result["api_contract"]["endpoints"], frontend_calls)
        return result

    def close(self):
        for _, service, _path in self.members:
            service.close()


def _tag(items: list[dict], repository: str) -> list[dict]:
    return [{**item, "repository": repository} for item in items]


def merge_knowledge_reports(reports: list[tuple[str, dict]], include_llm: bool = False) -> dict:
    """Merge serialized member reports while retaining repository provenance."""
    api_endpoints = []
    modules = []
    entities = []
    gaps = []
    violations = []
    config_files = []
    dependency_categories = []
    auth_entries = []
    hot_functions = []
    warm_functions = []
    semantic = defaultdict(list)
    totals = defaultdict(float)
    roles, permissions = set(), set()

    for repository, report in reports:
        api = report["api_contract"]
        api_endpoints.extend(_tag(api.get("endpoints", []), repository))
        topology = report["module_topology"]
        modules.extend(_tag(topology.get("modules", []), repository))
        totals["coupling"] += float(topology.get("coupling_score") or 0)
        entities.extend(_tag(report.get("core_entities", []), repository))
        coverage = report["test_coverage"]
        for key in ("test_functions", "production_functions", "covered_functions", "gap_count"):
            totals[key] += int(coverage.get(key) or 0)
        gaps.extend(_tag(coverage.get("top_gaps", []), repository))
        layers = report["layer_violations"]
        violations.extend(_tag(layers.get("violations", []), repository))
        config = report["config_consumption"]
        totals["config_keys"] += int(config.get("total_keys") or 0)
        totals["consumed_keys"] += int(config.get("consumed_keys") or 0)
        config_files.extend(_tag(config.get("files", []), repository))
        deps = report["external_dependencies"]
        dependency_categories.extend(_tag(deps.get("by_category", []), repository))
        totals["dependencies"] += int(deps.get("total_dependencies") or 0)
        auth = report["authorization_model"]
        totals["protected"] += int(auth.get("protected_endpoints") or 0)
        totals["middleware"] += int(auth.get("middleware_count") or 0)
        roles.update(auth.get("roles", []))
        permissions.update(auth.get("permissions", []))
        auth_entries.extend(_tag(auth.get("entries", []), repository))
        heat = report["heat_map"]
        for key in ("total_functions", "hot", "warm", "cold"):
            totals[key] += int(heat.get(key) or 0)
        hot_functions.extend(_tag(heat.get("hot_functions", []), repository))
        warm_functions.extend(_tag(heat.get("warm_functions", []), repository))
        if include_llm:
            for key in ("business_descriptions", "business_rules", "error_catalog", "state_machines"):
                semantic[key].extend(_tag(report.get(key, []), repository))

    production = int(totals["production_functions"])
    covered = int(totals["covered_functions"])
    disabled = {"note": "Set --llm flag to enable LLM analysis"}
    return {
        "repositories": [name for name, _ in reports],
        "api_contract": {"endpoint_count": len(api_endpoints), "endpoints": api_endpoints},
        "module_topology": {
            "module_count": len(modules),
            "coupling_score": round(totals["coupling"] / max(len(reports), 1), 3),
            "modules": modules,
            "dependencies": {},
        },
        "core_entities": sorted(entities, key=lambda item: -item.get("score", 0))[:30],
        "test_coverage": {
            "test_functions": int(totals["test_functions"]),
            "production_functions": production,
            "covered_functions": covered,
            "coverage_pct": round(covered / production * 100, 1) if production else 0,
            "gap_count": int(totals["gap_count"]),
            "top_gaps": gaps,
        },
        "layer_violations": {"violation_count": len(violations), "violations": violations},
        "config_consumption": {
            "config_files": len(config_files),
            "total_keys": int(totals["config_keys"]),
            "consumed_keys": int(totals["consumed_keys"]),
            "files": config_files,
        },
        "external_dependencies": {
            "categories": len(dependency_categories),
            "total_dependencies": int(totals["dependencies"]),
            "by_category": dependency_categories,
        },
        "authorization_model": {
            "protected_endpoints": int(totals["protected"]),
            "middleware_count": int(totals["middleware"]),
            "roles": sorted(roles),
            "permissions": sorted(permissions),
            "entries": auth_entries,
        },
        "heat_map": {
            key: int(totals[key]) for key in ("total_functions", "hot", "warm", "cold")
        }
        | {"hot_functions": hot_functions, "warm_functions": warm_functions},
        **{
            key: semantic[key] if include_llm else disabled
            for key in ("business_descriptions", "business_rules", "error_catalog", "state_machines")
        },
    }
