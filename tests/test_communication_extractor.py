from types import SimpleNamespace

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


def test_extractor_only_emits_entry_reachable_http_observations():
    artifact = CommunicationFactExtractor().collect(
        _Graph(), _Sources(), CollectorRuleSet("sha256:rules", "rules/v1"), snapshot_id="snapshot-1"
    )

    assert len(artifact.entries) == 1
    assert len(artifact.http_outbounds) == 1
    observation = artifact.http_outbounds[0]
    assert observation.entry_id == artifact.entries[0].entry_id
    assert observation.payload["request"]["authority"] == "users.internal"


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
