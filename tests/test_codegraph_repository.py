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
