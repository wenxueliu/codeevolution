import sqlite3

import pytest

from codehistory.codegraph_reader import CodeGraphReader
from codehistory.infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository, read_rows


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
