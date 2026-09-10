
from dataclasses import dataclass

from codeevolution.analysis.communication.extractor import CommunicationFactExtractor, _sanitize_url
from codeevolution.analysis.communication.schema import CollectorRuleSet
from codeevolution.domain.knowledge import CallTarget, EntryPointDef, FunctionDef


class _Graph:
    def __init__(self):
        self.entry = EntryPointDef("entry-node", "create", "app.create", "src/app.py", 1, "http", "GET", "/orders")
        self.handler = FunctionDef("entry-node", "create", "app.create", "src/app.py", "python", 1, 5, "function")
        self.client = FunctionDef("client-node", "call_users", "app.call_users", "src/app.py", "python", 7, 8, "function")

    def inbound_endpoints(self):
        return [self.entry]

    def functions(self):
        return [self.handler, self.client]

    def callees(self, node_id):
        if node_id == "entry-node":
            return [CallTarget("entry-node", "client-node", "requests.get", "function", "src/app.py", 7, 7)]
        return []

    def http_client_calls(self, pattern):
        if pattern == "requests.":
            return [{"caller_node_id": "client-node", "callee_node_id": "http-node", "callee_name": "requests.get", "file_path": "src/app.py", "call_line": 7}]
        return []

    def mq_producer_calls(self, pattern):
        return []

    def mq_consumers(self, pattern):
        return []

    def rpc_calls(self, pattern):
        return []

    def database_call_candidates(self):
        return []


class _Sources:
    def snippet(self, path, start, end):
        return 'requests.get("http://users.internal/users/1")'


@dataclass(frozen=True)
class _SourceFile:
    path: str


class _ManifestSources(_Sources):
    def list_files(self, categories=None, globs=None):
        return [_SourceFile("src/other.py")]


def test_extractor_only_emits_entry_reachable_http_observations():
    artifact = CommunicationFactExtractor().collect(
        _Graph(), _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )

    assert len(artifact.entries) == 1
    assert len(artifact.http_outbounds) == 1
    observation = artifact.http_outbounds[0]
    assert observation.entry_id == artifact.entries[0].entry_id
    assert observation.payload["request"]["authority"] == "users.internal"
    assert [item.node_id for item in observation.call_path] == ["entry-node", "client-node"]


def test_extractor_drops_test_entries_and_keeps_dynamic_calls_unresolved():
    graph = _Graph()
    graph.entry = EntryPointDef("test-entry", "test", "tests.test", "tests/test_app.py", 1, "http", "GET", "/test")
    graph.handler = FunctionDef("test-entry", "test", "tests.test", "tests/test_app.py", "python", 1, 2, "function")
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )

    assert artifact.entries == ()
    assert artifact.http_outbounds == ()


def test_http_url_sanitization_preserves_identity_but_not_credentials_or_query_values():
    assert _sanitize_url("https://user:secret@users.internal:8443/orders?id=42&token=hidden#fragment") == "https://users.internal:8443/orders?id&token"


def test_message_consumer_without_framework_entry_is_promoted_to_entry():
    graph = _Graph()
    consumer = FunctionDef("consumer-node", "consume", "app.consume", "src/consumer.py", "python", 20, 22, "function")
    graph.functions = lambda: [graph.handler, graph.client, consumer]
    graph.mq_consumers = lambda pattern: [{"node_id": "consumer-node", "name": "kafka.consume", "start_line": 20}] if pattern == "consume" else []
    graph.callees = lambda node_id: []
    sources = _Sources()
    artifact = CommunicationFactExtractor().collect(
        graph, sources, CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert any(item.kind == "message_consumer" for item in artifact.entries)
    assert artifact.message_subscriptions


def test_redis_pubsub_is_not_duplicated_as_resource_access():
    graph = _Graph()
    graph.mq_producer_calls = lambda pattern: [{
        "caller_node_id": "client-node", "callee_name": "redis.publish", "call_line": 7,
    }] if pattern == "publish" else []
    graph.database_call_candidates = lambda: [{
        "caller_node_id": "client-node", "name": "redis.publish", "call_line": 7,
    }]
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert artifact.message_publications
    assert artifact.resource_accesses == ()


def test_callsite_reachable_from_multiple_entries_is_emitted_per_entry():
    graph = _Graph()
    second = EntryPointDef("entry-two", "submit", "app.submit", "src/app.py", 2, "http", "POST", "/submit")
    graph.inbound_endpoints = lambda: [graph.entry, second]
    graph.callees = lambda node_id: [CallTarget(node_id, "client-node", "requests.get", "function", "src/app.py", 7, 7)] if node_id in {"entry-node", "entry-two"} else []
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert len(artifact.http_outbounds) == 2
    assert {item.entry_id for item in artifact.http_outbounds} == {item.entry_id for item in artifact.entries}
    assert len({item.observation_id for item in artifact.http_outbounds}) == 2


def test_reachability_budget_is_explicitly_truncated():
    artifact = CommunicationFactExtractor().collect(
        _Graph(), _Sources(), CollectorRuleSet("sha256:rules", "rules/v1", budgets={"call_depth": 0}), snapshot_id="snapshot-1"
    )
    http = next(item for item in artifact.collector_coverage if item.collector == "http")
    assert http.status.value == "truncated"
    assert http.coverage["reachability"][artifact.entries[0].entry_id]["truncated"] is True
    assert artifact.http_outbounds == ()


def test_locations_are_not_emitted_for_paths_outside_the_frozen_manifest():
    artifact = CommunicationFactExtractor().collect(
        _Graph(), _ManifestSources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert artifact.entries[0].handler.location is None
    assert artifact.http_outbounds[0].caller.location is None


def test_decorator_route_path_is_part_of_entry_identity():
    graph = _Graph()
    graph.entry = EntryPointDef(
        "entry-node", "create", "app.create", "src/app.py", 1, "http", decorators=["@router.post('/orders/{id}')"]
    )
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert artifact.entries[0].method == "POST"
    assert artifact.entries[0].path_template == "/orders/{id}"


def test_grpc_rows_with_frozen_caller_identity_are_collected():
    graph = _Graph()
    graph.rpc_calls = lambda pattern: [{
        "caller_node_id": "client-node", "callee_node_id": "rpc-node",
        "callee_name": "GetUser", "callee_qualified_name": "users.v1.UserServiceStub.GetUser",
        "call_line": 7,
    }] if pattern == "Stub" else []
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    assert artifact.grpc_clients
    assert artifact.grpc_clients[0].payload["rpc"]["identity_resolution"] == "generated_stub"
    assert artifact.grpc_clients[0].payload["rpc"]["fully_qualified_method"] == "/users.v1.UserService/GetUser"


def test_missing_protocol_adapter_is_unsupported_but_keeps_entry_inventory():
    graph = _Graph()
    graph.http_client_calls = None
    artifact = CommunicationFactExtractor().collect(
        graph, _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )
    http = next(item for item in artifact.collector_coverage if item.collector == "http")
    assert http.status.value == "unsupported"
    assert artifact.entries
