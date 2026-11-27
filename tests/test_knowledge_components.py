from codeevolution.analysis.knowledge.api_contract import ApiContractExtractor
from codeevolution.analysis.knowledge.authorization import AuthorizationExtractor
from codeevolution.analysis.knowledge.config_usage import ConfigUsageExtractor
from codeevolution.analysis.knowledge.core_entities import CoreEntityExtractor
from codeevolution.analysis.knowledge.dependencies import DependencyExtractor
from codeevolution.analysis.knowledge.heatmap import HeatmapExtractor
from codeevolution.analysis.knowledge.interface_contract import InterfaceContractExtractor
from codeevolution.analysis.knowledge.layer_rules import LayerRuleExtractor
from codeevolution.analysis.knowledge.module_topology import ModuleTopologyExtractor
from codeevolution.analysis.knowledge.report_builder import KnowledgeReportBuilder
from codeevolution.analysis.knowledge.semantic import SemanticExtractor
from codeevolution.knowledge import KnowledgeExtractor


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

    def api_call_chain_mermaid(self, _node_id):
        return "graph TD\n    N0[\"get_user_by_id\"]\n    N1[\"find_user\"]\n    N0 --> N1"

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
    assert contract.endpoints[0].node_id == "handler-1"
    assert contract.endpoints[1].node_id == "handler-1"
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

    # ProductService is a type but carries no positive entity evidence: the
    # service path and name penalties push it out of the candidate list.
    assert [entity.name for entity in entities] == ["Product"]
    assert all(entity.kind == "class" for entity in entities)
    assert entities[0].field_count == 8
    assert entities[0].language == "java"
    assert entities[0].score_breakdown["annotation_bonus"] == 10


def test_module_topology_extractor_owns_graph_building_and_analysis():
    extractor = ModuleTopologyExtractor(TopologyQueryStub())
    graph = extractor.build_graph()
    topology = extractor.extract(resolution=1.5)

    assert graph["src/users/api.py"]["src/users/service.py"]["weight"] == 2
    assert graph["src/users/service.py"]["src/orders/service.py"]["weight"] == 1
    assert sum(module["file_count"] for module in topology.modules) == 4
    assert 0 <= topology.coupling_score <= 1


class InterfaceSourceStub:
    def functions(self):
        return [
            {"name": "create_client", "kind": "function", "language": "python",
             "file_path": "src/pkg/client.py", "start_line": 3, "is_exported": 0,
             "node_id": "n1", "qualified_name": "src/pkg/client.py::create_client",
             "signature": "create_client(url: str) -> Client"},
            {"name": "_helper", "kind": "function", "language": "python",
             "file_path": "src/pkg/client.py", "start_line": 7},
            {"name": "load", "kind": "function", "language": "python",
             "file_path": "tests/helper.py", "start_line": 1},
            {"name": "hidden", "kind": "function", "language": "python",
             "file_path": "src/internal/secret.py", "start_line": 1},
            {"name": "run", "kind": "function", "language": "python",
             "file_path": "src/pkg/cli.py", "start_line": 1, "visibility": "private"},
            {"name": "fetch", "kind": "method", "language": "typescript",
             "file_path": "src/api.ts", "start_line": 10, "is_exported": 1},
            {"name": "internalFetch", "kind": "function", "language": "typescript",
             "file_path": "src/api.ts", "start_line": 12, "is_exported": 0},
        ]


def test_interface_contract_extractor_keeps_only_the_public_surface():
    entries = InterfaceContractExtractor(InterfaceSourceStub()).extract()
    assert [item["name"] for item in entries] == ["create_client", "fetch"]
    assert entries[0]["node_id"] == "n1"
    assert entries[0]["qualified_name"] == "src/pkg/client.py::create_client"
    assert entries[0]["signature"] == "create_client(url: str) -> Client"


class DeclaredExportStub:
    """A package __init__ that re-exports a class and a function."""

    def package_entry_imports(self):
        return [{
            "file_path": "pkg/__init__.py",
            "signature": "from .core import (\n    Client, create_client as build,\n)",
            "start_line": 1,
        }]

    def symbols_in_file(self, file_path):
        if file_path != "pkg/core.py":
            return []
        return [
            {"id": "c1", "kind": "class", "name": "Client", "qualified_name": "Client",
             "file_path": file_path, "language": "python", "start_line": 3,
             "signature": "class Client", "docstring": "A client."},
            {"id": "f1", "kind": "function", "name": "create_client",
             "qualified_name": "create_client", "file_path": file_path,
             "language": "python", "start_line": 12,
             "signature": "create_client(url: str) -> Client", "docstring": ""},
            {"id": "f2", "kind": "function", "name": "internal", "qualified_name": "internal",
             "file_path": file_path, "language": "python", "start_line": 20},
        ]

    def functions(self):
        raise AssertionError("declared exports must win over inference")

    def caller_edges(self):
        return [{
            "callee_id": "f1", "caller_id": "f2", "caller_name": "internal",
            "caller_qualified_name": "internal", "caller_file": "pkg/core.py",
            "caller_line": 20, "call_line": 25,
        }]


def test_interface_contract_prefers_declared_package_exports():
    entries = InterfaceContractExtractor(DeclaredExportStub()).extract()

    assert [item["name"] for item in entries] == ["Client", "build"]
    assert {item["public_surface"] for item in entries} == {"declared"}
    assert entries[0]["declared_by"] == "pkg/__init__.py"
    assert entries[0]["module"] == "pkg.core"
    # Declared exports stay public even when the package uses them internally.
    assert entries[1]["caller_count"] == 1


class RootSourceStub:
    def functions(self):
        return [
            {"name": "root", "kind": "function", "language": "python",
             "file_path": "pkg/api.py", "start_line": 1, "node_id": "root",
             "qualified_name": "root", "signature": "root()"},
            {"name": "helper", "kind": "function", "language": "python",
             "file_path": "pkg/api.py", "start_line": 5, "node_id": "helper",
             "qualified_name": "helper", "signature": "helper()"},
            {"name": "nested", "kind": "function", "language": "python",
             "file_path": "pkg/api.py", "start_line": 9, "node_id": "nested",
             "qualified_name": "root::nested", "signature": "nested()"},
        ]

    def caller_edges(self):
        return [{
            "callee_id": "helper", "caller_id": "root", "caller_name": "root",
            "caller_qualified_name": "root", "caller_file": "pkg/api.py",
            "caller_line": 1, "call_line": 6,
        }]

    def node_kind(self, qualified_name):
        return "function" if qualified_name == "root" else None


def test_interface_contract_keeps_only_call_graph_roots():
    entries = InterfaceContractExtractor(RootSourceStub()).extract()

    assert [item["name"] for item in entries] == ["root"]
    assert entries[0]["public_surface"] == "root"
    assert entries[0]["caller_count"] == 0


class InstantiatedClassStub:
    def functions(self):
        return [
            {"name": "execute", "kind": "method", "language": "python",
             "file_path": "pkg/service.py", "start_line": 4, "node_id": "m1",
             "qualified_name": "Service::execute", "signature": "execute(self)"},
        ]

    def caller_edges(self):
        return []

    def node_kind(self, qualified_name):
        return "class" if qualified_name == "Service" else None

    def instantiation_edges(self):
        return [{
            "class_name": "Service", "class_qualified_name": "Service",
            "caller_name": "build", "caller_qualified_name": "build",
            "caller_file": "pkg/app.py", "caller_line": 3, "call_line": 3,
        }]


def test_interface_contract_drops_methods_of_internally_instantiated_classes():
    assert InterfaceContractExtractor(InstantiatedClassStub()).extract() == []


def test_interface_contract_serialization_keeps_every_entry():
    entries = [
        {"name": f"entry_{index}", "kind": "function", "file_path": f"pkg/{index}.py",
         "public_surface": "root"}
        for index in range(250)
    ]

    payload = KnowledgeExtractor._serialize_interface(entries)

    assert payload["entry_count"] == 250
    assert len(payload["entries"]) == 250
    assert payload["truncated"] is False
    assert payload["inferred_count"] == 250


class HttpSurfaceStub:
    def functions(self):
        return [
            {"name": "root", "kind": "function", "language": "python",
             "file_path": "pkg/api.py", "start_line": 1, "node_id": "root",
             "qualified_name": "root", "signature": "root()"},
            {"name": "list_orders", "kind": "function", "language": "python",
             "file_path": "pkg/api.py", "start_line": 6, "node_id": "http1",
             "qualified_name": "list_orders", "signature": "list_orders()"},
        ]

    def caller_edges(self):
        return []

    def http_handler_node_ids(self):
        return ["http1"]


def test_interface_contract_does_not_repeat_http_handlers():
    entries = InterfaceContractExtractor(HttpSurfaceStub()).extract()

    assert [item["name"] for item in entries] == ["root"]


class DeclaredHttpHandlerStub(DeclaredExportStub):
    def http_handler_node_ids(self):
        return ["f1"]


def test_declared_export_that_is_an_http_handler_is_left_to_the_api_contract():
    entries = InterfaceContractExtractor(DeclaredHttpHandlerStub()).extract()

    assert [item["name"] for item in entries] == ["Client"]
