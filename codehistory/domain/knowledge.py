"""Knowledge and CodeGraph domain data transfer objects."""

from dataclasses import dataclass, field


@dataclass
class FunctionDef:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    kind: str
    signature: str | None = None
    visibility: str | None = None
    is_exported: bool = False
    is_async: bool = False
    is_static: bool = False
    is_test: bool = False
    parent_class: str | None = None
    decorators: list[str] = field(default_factory=list)


@dataclass
class CallTarget:
    caller_node_id: str
    callee_node_id: str
    callee_name: str
    callee_kind: str
    callee_file: str
    callee_line: int
    call_line: int
    provenance: str | None = None


@dataclass
class EntryPointDef:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    entry_type: str
    http_method: str | None = None
    http_path: str | None = None
    params: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    call_tree: list[str] = field(default_factory=list)


@dataclass
class ApiEndpoint:
    method: str
    path: str
    handler_name: str
    file_path: str
    line: int
    params: list[str] = field(default_factory=list)
    return_type: str | None = None
    decorators: list[str] = field(default_factory=list)
    downstream_calls: list[str] = field(default_factory=list)
    request_headers: list[dict] = field(default_factory=list)
    query_params: list[dict] = field(default_factory=list)
    path_params: list[dict] = field(default_factory=list)
    request_body: dict | None = None
    response_body: dict | None = None
    call_chain: list[dict] = field(default_factory=list)
    call_chain_mermaid: str = ""
    frontend_callers: list[dict] = field(default_factory=list)


@dataclass
class ApiContract:
    endpoints: list[ApiEndpoint] = field(default_factory=list)
    resource_groups: dict[str, list[ApiEndpoint]] = field(default_factory=dict)


@dataclass
class ModuleTopology:
    modules: list[dict] = field(default_factory=list)
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    coupling_score: float = 0.0


@dataclass
class CoreEntity:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    kind: str
    pagerank: float
    in_degree: int
    out_degree: int
    layer: str = ""
    field_count: int = 0
    relationship_count: int = 0
    start_line: int = 0
    score: float = 0.0
    annotations: list[str] = field(default_factory=list)


@dataclass
class TestGap:
    node_id: str
    name: str
    qualified_name: str
    file_path: str
    kind: str
    line: int
    is_exported: bool = False


@dataclass
class LayerViolation:
    source_name: str
    source_file: str
    source_layer: str
    target_name: str
    target_file: str
    target_layer: str
    call_line: int | None = None
