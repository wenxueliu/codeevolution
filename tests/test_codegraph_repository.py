import sqlite3
from pathlib import Path

import pytest

from codeevolution.codegraph_reader import CodeGraphReader
from codeevolution.infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository, read_rows


def _empty_codegraph(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE nodes (
            id TEXT, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT,
            language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            visibility TEXT, is_exported INTEGER, is_async INTEGER,
            is_static INTEGER, decorators TEXT
        );
        CREATE TABLE edges (
            id TEXT, source TEXT, target TEXT, kind TEXT, metadata TEXT,
            line INTEGER, col INTEGER, provenance TEXT
        );
        CREATE TABLE files (
            path TEXT, content_hash TEXT, language TEXT, size INTEGER,
            modified_at TEXT, indexed_at TEXT, node_count INTEGER
        );
        """
    )
    connection.close()


def test_repository_typed_queries_and_legacy_facade_match(tmp_path):
    database = tmp_path / "codegraph.db"
    _empty_codegraph(database)
    with SQLiteCodeGraphRepository(str(database)) as repository:
        assert repository.functions() == []
        assert repository.inbound_endpoints() == []
        assert repository.route_nodes() == []
        assert repository.decorated_handlers() == []
        assert repository.http_handler_endpoints() == []
        assert repository.module_import_edges() == []
        assert repository.cross_file_call_edges() == []
        assert repository.call_edges() == []
        assert repository.layer_call_edges() == []
        assert repository.file_records() == []
        assert repository.config_candidate_nodes() == []
        assert repository.import_nodes() == []
        assert repository.decorator_nodes() == []
        assert repository.authorization_handlers() == []
        assert repository.authorization_middleware() == []
        assert repository.enum_nodes() == []
        assert repository.enum_members("missing") == []
        assert repository.functions_named_like("missing") == []
        assert repository.database_call_candidates() == []
        assert repository.primary_language() == ""
        assert not repository.has_node_name("missing")
        assert repository.http_client_calls("missing") == []
        assert repository.function_location("missing") == []
        assert repository.url_candidate_nodes("missing.py", 1, 2) == []
        assert repository.mq_producer_calls("missing") == []
        assert repository.mq_consumers("missing") == []
        assert repository.topic_candidate_nodes("missing.py") == []
        assert repository.rpc_calls("missing") == []
        assert repository.business_entity_nodes() == []
        assert repository.query("SELECT * FROM files") == []

    with CodeGraphReader(str(database)) as legacy:
        assert legacy.get_all_functions() == []


def test_context_manager_closes_on_exception(tmp_path):
    database = tmp_path / "codegraph.db"
    _empty_codegraph(database)
    repository = SQLiteCodeGraphRepository(str(database))
    with pytest.raises(RuntimeError):
        with repository:
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        repository.conn.execute("SELECT 1")


def test_read_rows_uses_read_only_connection(tmp_path):
    database = tmp_path / "codegraph.db"
    _empty_codegraph(database)
    assert read_rows(str(database), "SELECT path FROM files") == []
    with pytest.raises(sqlite3.OperationalError):
        read_rows(str(database), "INSERT INTO files(path) VALUES ('x')")


def test_callers_depth_and_chain_semantics(tmp_path):
    database = tmp_path / "codegraph.db"
    _empty_codegraph(database)
    connection = sqlite3.connect(database)
    nodes = [
        ("a", "function", "caller", "m::caller", "m.py", "python", 1, 2),
        ("b", "function", "middle", "m::middle", "m.py", "python", 3, 4),
        ("c", "function", "leaf", "m::leaf", "m.py", "python", 5, 6),
    ]
    connection.executemany(
        """INSERT INTO nodes(
               id, kind, name, qualified_name, file_path, language, start_line, end_line,
               is_exported, is_async, is_static
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)""",
        nodes,
    )
    connection.executemany(
        "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', ?)",
        [("ab", "a", "b", 2), ("bc", "b", "c", 4)],
    )
    connection.commit()
    connection.close()

    with CodeGraphReader(str(database)) as reader:
        assert reader.get_callers("b")[0].callee_name == "caller"
        assert reader.get_call_tree("a", max_depth=1) == ["a", "b"]
        assert reader.get_call_tree("a", max_depth=0) == ["a"]
        assert reader.get_call_chain("a")[0]["from"] == "caller"


def test_repository_contract_tolerates_null_and_bad_decorator_json(tmp_path):
    database = tmp_path / "codegraph.db"
    _empty_codegraph(database)
    connection = sqlite3.connect(database)
    connection.executemany(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, is_exported, is_async, is_static, decorators)
           VALUES (?, 'function', ?, ?, 'app.py', 'python', 1, 2, 0, 0, 0, ?)""",
        [
            ("null", "null_decorators", "app::null", None),
            ("bad", "bad_decorators", "app::bad", "{not-json"),
        ],
    )
    connection.commit()
    connection.close()

    with SQLiteCodeGraphRepository(str(database)) as repository:
        functions = repository.functions()
    assert [function.decorators for function in functions] == [[], []]


def test_metadata_contract_tolerates_older_schema_and_missing_tables(tmp_path):
    old_database = tmp_path / "old.db"
    connection = sqlite3.connect(old_database)
    connection.executescript(
        """CREATE TABLE files(path TEXT, language TEXT);
           CREATE TABLE nodes(id TEXT, kind TEXT, name TEXT);"""
    )
    connection.execute("INSERT INTO files VALUES ('app.py', 'python')")
    connection.execute("INSERT INTO nodes VALUES ('i', 'import', 'redis')")
    connection.commit()
    connection.close()
    with SQLiteCodeGraphRepository(str(old_database)) as repository:
        metadata = repository.inspect_metadata()
    assert metadata["language"] == "python"
    assert metadata["imports"] == [{"name": "redis", "signature": None}]
    assert metadata["edges"] == 0
    assert metadata["indexed_at"] is None

    empty_database = tmp_path / "empty.db"
    sqlite3.connect(empty_database).close()
    with SQLiteCodeGraphRepository(str(empty_database)) as repository:
        assert repository.inspect_metadata()["nodes"] == 0


def _seed_handler_repo(root: Path) -> str:
    """Write a stdlib http.server handler repo + a matching codegraph DB.

    db_path lives at ``root/.codegraph/codegraph.db`` so the repository resolves
    ``Path(db_path).parent.parent`` back to ``root`` (the CodeGraph repo root).
    """
    (root / ".codegraph").mkdir(parents=True, exist_ok=True)
    (root / "webapi.py").write_text(
        """\
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/workflows":
            return self._list_workflows()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        if path.startswith("/api/workflow/") and path.endswith("/control"):
            req_id = path.split("/")[-2]
            return self._control(req_id, body)
""",
        encoding="utf-8",
    )
    # a test-file handler that imports http.server — must be excluded
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "stub.py").write_text(
        "from http.server import BaseHTTPRequestHandler\n"
        "class Stub(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path == '/api/test':\n"
        "            return self._x()\n",
        encoding="utf-8",
    )

    db = root / ".codegraph" / "codegraph.db"
    _empty_codegraph(db)
    connection = sqlite3.connect(db)
    connection.executemany(
        """INSERT INTO nodes(
               id, kind, name, qualified_name, file_path, language,
               start_line, end_line, signature
           ) VALUES (?, ?, ?, ?, ?, 'python', 1, 2, ?)""",
        [
            # production handler
            ("c1", "class", "Api", "Api", "webapi.py", None),
            ("m1", "method", "do_GET", "Api::do_GET", "webapi.py", None),
            ("m2", "method", "do_POST", "Api::do_POST", "webapi.py", None),
            ("i1", "import", "http.server", "http.server", "webapi.py",
             "from http.server import BaseHTTPRequestHandler"),
            # test-file handler (must be ignored)
            ("c2", "class", "Stub", "Stub", "tests/stub.py", None),
            ("m3", "method", "do_GET", "Stub::do_GET", "tests/stub.py", None),
            ("i2", "import", "http.server", "http.server", "tests/stub.py",
             "from http.server import BaseHTTPRequestHandler"),
        ],
    )
    connection.executemany(
        "INSERT INTO edges(id, source, target, kind) VALUES (?, ?, ?, 'contains')",
        [("e1", "c1", "m1"), ("e2", "c1", "m2"), ("e3", "c2", "m3")],
    )
    connection.commit()
    connection.close()
    return str(db)


def test_http_handler_endpoints_reconstruct_from_seeded_repo(tmp_path):
    db = _seed_handler_repo(tmp_path)
    with SQLiteCodeGraphRepository(db) as repository:
        endpoints = repository.http_handler_endpoints()

    assert [(e["method"], e["path"], e["handler_name"], e["params"]) for e in endpoints] == [
        ("GET", "/api/workflows", "_list_workflows", []),
        ("POST", "/api/workflow/{req_id}/control", "_control", ["req_id"]),
    ]
    # provenance marker names the source class
    assert all(e["decorators"] == ["http.server(源自 Api)"] for e in endpoints)
    # test-file handlers are never surfaced
    assert all(e["file_path"] == "webapi.py" for e in endpoints)
    # the POST handler reads a JSON body (do_POST prologue) → tagged as JSON;
    # the GET handler reads no payload → stays bodyless
    by_path = {e["path"]: e for e in endpoints}
    assert by_path["/api/workflow/{req_id}/control"]["request_body"] == {
        "type": "JSON 对象",
        "format": "application/json",
    }
    assert by_path["/api/workflows"]["request_body"] is None
