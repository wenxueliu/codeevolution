"""Compatibility facade for composable knowledge extractors."""

from collections import defaultdict
from pathlib import Path

import networkx as nx

from .analysis.knowledge.api_contract import ApiContractExtractor
from .analysis.knowledge.authorization import AuthorizationExtractor
from .analysis.knowledge.config_usage import ConfigUsageExtractor
from .analysis.knowledge.core_entities import CoreEntityExtractor
from .analysis.knowledge.dependencies import DependencyExtractor
from .analysis.knowledge.heatmap import HeatmapExtractor
from .analysis.knowledge.layer_rules import LayerRuleExtractor
from .analysis.knowledge.module_topology import ModuleTopologyExtractor
from .analysis.knowledge.semantic import SemanticExtractor
from .analysis.knowledge.test_gaps import TestGapExtractor
from .codegraph_reader import CodeGraphReader, FunctionDef
from .domain.knowledge import (
    ApiContract,
    ApiEndpoint,
    CoreEntity,
    LayerViolation,
    ModuleTopology,
    TestGap,
)
from .infrastructure.source_filesystem import FileSystemSourceProvider
from .ports import SourceProvider


class KnowledgeExtractor:
    """Legacy public API delegating each dimension to an independent analyzer."""

    def __init__(self, reader: CodeGraphReader, source_provider: SourceProvider | None = None):
        self.reader = reader
        repo_root = Path(reader.db_path).parent.parent
        self.source_provider = source_provider or FileSystemSourceProvider(repo_root)
        self._api = ApiContractExtractor(reader)
        self._modules = ModuleTopologyExtractor(reader)
        self._layers = LayerRuleExtractor(reader)
        self._entities = CoreEntityExtractor(reader, self._layers.classify_file)
        self._gaps = TestGapExtractor(reader)
        self._config = ConfigUsageExtractor(reader, self.source_provider)
        self._dependencies = DependencyExtractor(reader)
        self._authorization = AuthorizationExtractor(reader)
        self._heatmap = HeatmapExtractor(reader, self._get_call_graph, self._layers.classify_file)
        self._semantic = SemanticExtractor(
            reader, self.source_provider, self.extract_core_entities, self._layers.classify_file
        )

    def extract_api_contract(self) -> ApiContract:
        return self._api.extract()

    def extract_module_topology(self, resolution: float = 0.8) -> ModuleTopology:
        return self._modules.extract(resolution)

    def extract_core_entities(self, top_n: int = 30) -> list[CoreEntity]:
        return self._entities.extract(top_n)

    def extract_test_gaps(self) -> list[TestGap]:
        return self._gaps.extract()

    def extract_test_coverage_stats(self) -> dict:
        return self._gaps.coverage_stats()

    def extract_layer_violations(self) -> list[LayerViolation]:
        return self._layers.extract()

    def extract_config_consumption(self) -> list[dict]:
        return self._config.extract()

    def extract_external_dependencies(self) -> list[dict]:
        return self._dependencies.extract()

    def extract_authorization_model(self) -> list[dict]:
        return self._authorization.extract()

    def extract_heat_map(self) -> list[dict]:
        return self._heatmap.extract()

    def extract_business_descriptions(self, func_names=None, limit=20):
        return self._semantic.business_descriptions(func_names, limit)

    def extract_business_rules_llm(self, func_names=None, limit=15):
        return self._semantic.business_rules(func_names, limit)

    def extract_error_catalog(self, func_names=None, limit=20):
        return self._semantic.error_catalog(func_names, limit)

    def extract_state_machines(self):
        return self._semantic.state_machines()

    def extract_all(self, include_llm: bool = False) -> dict:
        api = self.extract_api_contract()
        modules = self.extract_module_topology()
        entities = self.extract_core_entities(30)
        gaps = self.extract_test_gaps()
        violations = self.extract_layer_violations()
        disabled = {"note": "Set --llm flag to enable LLM analysis"}
        return {
            "api_contract": {
                "endpoint_count": len(api.endpoints),
                "endpoints": [
                    {
                        "method": item.method,
                        "path": item.path,
                        "handler": item.handler_name,
                        "file": item.file_path,
                        "line": item.line,
                        "params": item.params,
                        "return_type": item.return_type,
                        "request_headers": item.request_headers,
                        "query_params": item.query_params,
                        "path_params": item.path_params,
                        "request_body": item.request_body,
                        "response_body": item.response_body,
                        "call_chain": item.call_chain,
                        "frontend_callers": item.frontend_callers,
                    }
                    for item in api.endpoints[:100]
                ],
                "resource_groups": {
                    name: [
                        {"method": item.method, "path": item.path, "handler": item.handler_name}
                        for item in items[:10]
                    ]
                    for name, items in api.resource_groups.items()
                },
            },
            "module_topology": {
                "module_count": len(modules.modules),
                "coupling_score": modules.coupling_score,
                "modules": [
                    {key: module[key] for key in ("id", "name", "file_count", "primary_language")}
                    for module in modules.modules
                ],
                "dependencies": modules.dependency_graph,
            },
            "core_entities": [
                {
                    "name": item.name,
                    "qualified_name": item.qualified_name,
                    "file_path": item.file_path,
                    "kind": item.kind,
                    "pagerank": item.pagerank,
                    "in_degree": item.in_degree,
                    "out_degree": item.out_degree,
                    "layer": item.layer,
                    "field_count": item.field_count,
                    "relationship_count": item.relationship_count,
                    "score": item.score,
                    "annotations": item.annotations,
                }
                for item in entities
            ],
            "test_coverage": {
                **self.extract_test_coverage_stats(),
                "top_gaps": [
                    {
                        "name": item.name,
                        "qualified_name": item.qualified_name,
                        "file_path": item.file_path,
                        "kind": item.kind,
                        "line": item.line,
                        "is_exported": item.is_exported,
                    }
                    for item in gaps[:50]
                ],
            },
            "layer_violations": {
                "violation_count": len(violations),
                "violations": [
                    {
                        "source": item.source_name,
                        "source_file": item.source_file,
                        "source_layer": item.source_layer,
                        "target": item.target_name,
                        "target_file": item.target_file,
                        "target_layer": item.target_layer,
                    }
                    for item in violations[:50]
                ],
            },
            "config_consumption": self._serialize_config_consumption(
                self.extract_config_consumption()
            ),
            "external_dependencies": self._serialize_external_deps(
                self.extract_external_dependencies()
            ),
            "authorization_model": self._serialize_auth_model(self.extract_authorization_model()),
            "heat_map": self._serialize_heat_map(self.extract_heat_map()),
            "business_descriptions": self.extract_business_descriptions(limit=15)
            if include_llm
            else disabled,
            "business_rules": self.extract_business_rules_llm(limit=10)
            if include_llm
            else disabled,
            "error_catalog": self.extract_error_catalog(limit=15) if include_llm else disabled,
            "state_machines": self.extract_state_machines() if include_llm else disabled,
        }

    # Legacy helper entry points retained for callers that used implementation details.
    _infer_path = staticmethod(ApiContractExtractor.infer_path)
    _resource_prefix = staticmethod(ApiContractExtractor.resource_prefix)
    _parse_params = staticmethod(ApiContractExtractor.parse_params)
    _parse_return_type = staticmethod(ApiContractExtractor.parse_return_type)
    _common_prefix = staticmethod(ModuleTopologyExtractor.common_prefix)
    _is_test_function = staticmethod(TestGapExtractor.is_test_function)
    _classify_file_layer = staticmethod(LayerRuleExtractor.classify_file)
    _classify_import = staticmethod(DependencyExtractor.classify)
    _extract_enum_usage_lines = staticmethod(SemanticExtractor.enum_usage)

    def _build_module_graph(self):
        return self._modules.build_graph()

    def _get_call_graph(self):
        return self._entities.call_graph()

    def _extract_config_keys(self, file_path):
        return self._config.extract_keys(file_path)

    def _read_source_snippet(self, file_path, start_line, end_line, context_lines=5):
        return self.source_provider.snippet(
            file_path, max(1, start_line - context_lines), end_line + context_lines
        )

    def _query(self, sql, params=None):
        return self.reader.query(sql, params)

    @staticmethod
    def _pagerank_python(graph: nx.DiGraph, alpha=0.85, max_iter=100, tol=1e-6):
        return CoreEntityExtractor.pagerank(graph, alpha, max_iter, tol)

    @staticmethod
    def _serialize_config_consumption(data):
        return {
            "config_files": len(data),
            "total_keys": sum(item["key_count"] for item in data),
            "consumed_keys": sum(item["consumed_keys"] for item in data),
            "files": data[:20],
        }

    @staticmethod
    def _serialize_external_deps(data):
        return {
            "categories": len(data),
            "total_dependencies": sum(item["dependency_count"] for item in data),
            "by_category": data,
        }

    @staticmethod
    def _serialize_auth_model(data):
        endpoints = [item for item in data if item["auth_level"] != "middleware"]
        return {
            "protected_endpoints": len(endpoints),
            "middleware_count": len(data) - len(endpoints),
            "roles": sorted({role for item in data for role in item["roles"]}),
            "permissions": sorted(
                {permission for item in data for permission in item["permissions"]}
            ),
            "entries": data[:100],
        }

    @staticmethod
    def _serialize_heat_map(data):
        counts = defaultdict(int)
        for item in data:
            counts[item["heat"]] += 1
        return {
            "total_functions": len(data),
            "hot": counts["hot"],
            "warm": counts["warm"],
            "cold": counts["cold"],
            "hot_functions": [item for item in data if item["heat"] == "hot"][:30],
            "warm_functions": [item for item in data if item["heat"] == "warm"][:30],
        }


__all__ = [
    "ApiContract",
    "ApiEndpoint",
    "CoreEntity",
    "FunctionDef",
    "KnowledgeExtractor",
    "LayerViolation",
    "ModuleTopology",
    "TestGap",
]
