from codehistory.analysis.knowledge.api_contract import ApiContractExtractor
from codehistory.analysis.knowledge.authorization import AuthorizationExtractor
from codehistory.analysis.knowledge.config_usage import ConfigUsageExtractor
from codehistory.analysis.knowledge.core_entities import CoreEntityExtractor
from codehistory.analysis.knowledge.dependencies import DependencyExtractor
from codehistory.analysis.knowledge.heatmap import HeatmapExtractor
from codehistory.analysis.knowledge.layer_rules import LayerRuleExtractor
from codehistory.analysis.knowledge.module_topology import ModuleTopologyExtractor
from codehistory.analysis.knowledge.report_builder import KnowledgeReportBuilder
from codehistory.analysis.knowledge.semantic import SemanticExtractor
from codehistory.analysis.knowledge.test_gaps import TestGapExtractor as GapExtractor


class QueryStub:
    def route_nodes(self):
        return [{"name": "GET /api/users", "file_path": "routes.py", "start_line": 3}]

    def decorated_handlers(self):
        return [
            {
                "id": "handler-1",
                "name": "get_user_by_id",
                "qualified_name": "routes.py::get_user_by_id",
                "file_path": "routes.py",
                "start_line": 8,
                "signature": "(self, user_id: str) -> User",
                "decorators": '["@router.get(\\"/api/users/{user_id}\\")"]',
            }
        ]

    def handler_for_route(self, _file_path, _route_line):
        return {
            "id": "handler-1",
            "name": "get_user_by_id",
            "qualified_name": "routes.py::get_user_by_id",
            "file_path": "routes.py",
            "start_line": 8,
            "signature": 'CommonResult<User> (@RequestHeader("Authorization") String token, @PathVariable Long user_id, @RequestBody UserRequest request)',
            "decorators": None,
        }

    def api_call_chain(self, _node_id):
        return [{"id": "handler-1", "name": "get_user_by_id"}, {"id": "service", "name": "find_user"}]

    def type_schema(self, type_name):
        return {"name": type_name, "fields": [{"name": "id", "signature": "Long id"}]}


class DomainQueryStub:
    def domain_type_nodes(self):
        return [
            {"id": "product", "kind": "class", "name": "Product", "qualified_name": "domain::Product", "file_path": "src/domain/Product.java", "decorators": '[@Entity]'},
            {"id": "service", "kind": "class", "name": "ProductService", "qualified_name": "service::ProductService", "file_path": "src/service/ProductService.java", "decorators": None},
        ]

    def domain_type_relationships(self):
        return [{"source": "service", "target": "product", "kind": "references"}]

    def domain_type_field_counts(self):
        return [{"id": "product", "field_count": 8}, {"id": "service", "field_count": 1}]


class TopologyQueryStub:
    def module_import_edges(self):
        return [
            {"f1": "src/users/api.py", "f2": "src/users/service.py"},
            {"f1": "src/orders/api.py", "f2": "src/orders/service.py"},
        ]

    def cross_file_call_edges(self):
        return [{"f1": "src/users/service.py", "f2": "src/orders/service.py"}]


def test_report_builder_runs_dimensions_in_declared_order():
    calls = []
    builder = KnowledgeReportBuilder(
        {
            "api_contract": ApiContractExtractor(lambda: calls.append("api") or {"count": 1}),
            "layers": ApiContractExtractor(lambda: calls.append("layers") or []),
        }
    )

    assert builder.build() == {"api_contract": {"count": 1}, "layers": []}
    assert calls == ["api", "layers"]


def test_every_knowledge_dimension_is_independently_callable():
    dimensions = [
        AuthorizationExtractor,
        ConfigUsageExtractor,
        CoreEntityExtractor,
        DependencyExtractor,
        HeatmapExtractor,
        LayerRuleExtractor,
        ModuleTopologyExtractor,
        SemanticExtractor,
        GapExtractor,
    ]
    for dimension in dimensions:
        assert dimension(lambda: "result").extract() == "result"


def test_api_contract_extractor_owns_the_real_extraction_algorithm():
    contract = ApiContractExtractor(QueryStub()).extract()

    assert [(endpoint.method, endpoint.path) for endpoint in contract.endpoints] == [
        ("GET", "/api/users"),
        ("GET", "/api/users/{user_id}"),
    ]
    assert contract.endpoints[1].params == ["user_id"]
    assert contract.endpoints[1].return_type == "User"
    assert contract.endpoints[0].request_headers[0]["name"] == "Authorization"
    assert contract.endpoints[0].request_body["type"] == "UserRequest"
    assert contract.endpoints[0].response_body["type"] == "CommonResult<User>"
    assert [node["name"] for node in contract.endpoints[0].call_chain] == [
        "get_user_by_id",
        "find_user",
    ]
    assert list(contract.resource_groups) == ["users"]


def test_core_entities_are_domain_types_not_methods():
    entities = CoreEntityExtractor(DomainQueryStub()).extract()

    assert [entity.name for entity in entities] == ["Product", "ProductService"]
    assert all(entity.kind == "class" for entity in entities)
    assert entities[0].field_count == 8


def test_module_topology_extractor_owns_graph_building_and_analysis():
    extractor = ModuleTopologyExtractor(TopologyQueryStub())
    graph = extractor.build_graph()
    topology = extractor.extract(resolution=1.5)

    assert graph["src/users/api.py"]["src/users/service.py"]["weight"] == 2
    assert graph["src/users/service.py"]["src/orders/service.py"]["weight"] == 1
    assert sum(module["file_count"] for module in topology.modules) == 4
    assert 0 <= topology.coupling_score <= 1
