"""Pure-AST endpoint synthesis for stdlib ``http.server`` dispatch handlers.

The synthesizer (`codeevolution.analysis.knowledge.http_server`) reconstructs
URL templates from ``BaseHTTPRequestHandler.do_<VERB>`` methods that hand-
dispatch on ``self.path``. It takes source text only — no codegraph database —
so these tests are inline-string driven, mirroring the shapes seen in
``harness_framework/webapi.py``.
"""

import ast

from codeevolution.analysis.knowledge.http_server import (
    EndpointSpec,
    handler_class_names,
    synthesize_class_endpoints,
)


def _specs(source: str, class_name: str) -> list[EndpointSpec]:
    return synthesize_class_endpoints(source, class_name)


def _paths(source: str, class_name: str) -> list[str]:
    return [s.path for s in _specs(source, class_name)]


def test_exact_path_equality_endpoints():
    source = '''\
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path == "/api/workflows":
            return self._list_workflows()
        if path == "/api/health":
            return self._send_json(200, {"ok": True})
'''
    assert handler_class_names(ast.parse(source)) == ["Api"]
    specs = _specs(source, "Api")
    assert [(s.method, s.path, s.handler, s.note) for s in specs] == [
        ("GET", "/api/workflows", "_list_workflows", "exact"),
        ("GET", "/api/health", "Api.do_GET", "exact"),
    ]


def test_prefix_plus_open_ended_tail_parameter():
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path.startswith("/api/workflow/"):
            parts = path.split("/")
            req_id = parts[-1]
            return self._get_workflow(req_id)
'''
    specs = _specs(source, "Api")
    assert [(s.method, s.path, s.params, s.note) for s in specs] == [
        ("GET", "/api/workflow/{req_id}", ["req_id"], "tail")
    ]


def test_prefix_token_and_segment_params_produce_parameterised_path():
    # /api/workflow/<req_id>/task/<task_name>/messages (webapi task-messages shape)
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path.startswith("/api/workflow/"):
            parts = path.split("/")
            if (len(parts) == 7 and parts[4] == "task"
                    and parts[6] == "messages"):
                return self._get_task_messages(parts[3], parts[5])

    def _get_task_messages(self, req_id, task_name):
        return None
'''
    specs = _specs(source, "Api")
    assert [(s.method, s.path, s.params, s.handler, s.note) for s in specs] == [
        (
            "GET",
            "/api/workflow/{req_id}/task/{task_name}/messages",
            ["req_id", "task_name"],
            "_get_task_messages",
            "segments",
        )
    ]


def test_prefix_suffix_endswith_produces_parameterised_post():
    # POST /api/workflow/<req_id>/control
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_POST(self):
        path = self.path.rstrip("/")
        if path.startswith("/api/workflow/") and path.endswith("/control"):
            req_id = path.split("/")[-2]
            return self._control(req_id, body)
'''
    specs = _specs(source, "Api")
    assert [(s.method, s.path, s.params, s.note) for s in specs] == [
        ("POST", "/api/workflow/{req_id}/control", ["req_id"], "prefix+suffix")
    ]


def test_do_options_never_yields_endpoints():
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/health":
            return self._send_json(200, {"ok": True})
'''
    specs = _specs(source, "Api")
    assert specs and all(s.method != "OPTIONS" for s in specs)
    # an OPTIONS-only class is not even a candidate
    assert handler_class_names(ast.parse('''\
class OnlyOptions(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
''')) == []


def test_self_contradictory_runs_branch_is_dropped_as_dead_code():
    # parts[2] sits on the static base ('workflow') — the runs-family branch is
    # unreachable in webapi (it reads the literal base segment as the req_id).
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path.startswith("/api/workflow/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[3] == "runs":
                return self._get_run(parts[2], parts[4])
            req_id = parts[-1]
            return self._get_workflow(req_id)

    def _get_run(self, req_id, run_id):
        return None
'''
    paths = _paths(source, "Api")
    assert paths == ["/api/workflow/{req_id}"]
    assert not any("runs" in p for p in paths)


def test_helper_param_names_backfilled_for_bare_parts_arguments():
    # helper consumes parts[3] positionally; name comes from its def signature
    source = '''\
class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path.startswith("/api/workflow/"):
            parts = path.split("/")
            if len(parts) == 4:
                return self._get_workflow(parts[3])

    def _get_workflow(self, req_id):
        return None
'''
    specs = _specs(source, "Api")
    assert [(s.path, s.params, s.note) for s in specs] == [
        ("/api/workflow/{req_id}", ["req_id"], "segments")
    ]


def _kinds(source: str, class_name: str) -> list[str]:
    return [s.request_body_kind for s in _specs(source, class_name)]


def test_json_body_prologue_tags_post_endpoint():
    source = '''\
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse
import json

class Api(BaseHTTPRequestHandler):
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        if path.startswith("/api/workflow/") and path.endswith("/control"):
            req_id = path.split("/")[-2]
            return self._control(req_id, body)

    def _control(self, req_id, body):
        return None
'''
    specs = _specs(source, "Api")
    assert [(s.method, s.path, s.request_body_kind) for s in specs] == [
        ("POST", "/api/workflow/{req_id}/control", "json")
    ]


def test_bytes_body_prologue_tags_put_endpoint():
    source = '''\
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

class Store(BaseHTTPRequestHandler):
    def do_PUT(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if path.startswith("/v1/kv/"):
            key = path[len("/v1/kv/"):]
            return self._handle_kv_put(key, body)

    def _handle_kv_put(self, key, body):
        return None
'''
    specs = _specs(source, "Store")
    assert [(s.method, s.path, s.request_body_kind) for s in specs] == [
        ("PUT", "/v1/kv/{key}", "bytes")
    ]


def test_handlers_without_payload_leave_body_kind_empty():
    # do_GET reads no body; a do_PUT branch that ignores the payload is empty.
    source = '''\
from http.server import BaseHTTPRequestHandler

class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/workflows":
            return self._list_workflows()

    def do_DELETE(self):
        path = self.path.rstrip("/")
        if path.startswith("/v1/kv/"):
            key = path.split("/")[-1]
            return self._delete_kv(key)

    def _list_workflows(self):
        return None

    def _delete_kv(self, key):
        return None
'''
    assert _kinds(source, "Api") == ["", ""]
