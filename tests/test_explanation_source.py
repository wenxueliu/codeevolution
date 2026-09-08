import sqlite3

from codeevolution.infrastructure import explanation_source


def test_source_loader_freezes_full_reachable_functions(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    graph_dir = repo / ".codegraph"
    graph_dir.mkdir(parents=True)
    (repo / "orders.py").write_text(
        "def create_order():\n    return save_order()\n\ndef save_order():\n    return 1\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(graph_dir / "codegraph.db")
    connection.executescript(
        """
        CREATE TABLE nodes (
          id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
          file_path TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
          signature TEXT, visibility TEXT, is_exported INTEGER, is_async INTEGER,
          is_static INTEGER, decorators TEXT
        );
        CREATE TABLE edges (
          id TEXT PRIMARY KEY, source TEXT, target TEXT, kind TEXT,
          metadata TEXT, line INTEGER, col INTEGER, provenance TEXT
        );
        """
    )
    rows = [
        ("root-id", "function", "create_order", "orders::create_order", "orders.py", "python", 1, 2, "()", "public", 1, 0, 0, "[]"),
        ("leaf-id", "function", "save_order", "orders::save_order", "orders.py", "python", 4, 5, "()", "private", 0, 0, 0, "[]"),
    ]
    connection.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.execute(
        "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)",
        ("edge", "root-id", "leaf-id", "calls", "{}", 2, 12, "ast"),
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(explanation_source, "get_repo", lambda name: {"name": name})
    monkeypatch.setattr(
        explanation_source,
        "repository_members",
        lambda entry: [{"name": "orders", "path": str(repo)}],
    )
    monkeypatch.setattr(explanation_source.RepositoryExplanationSource, "_revision", lambda self, root: "rev")

    frozen = explanation_source.RepositoryExplanationSource().load(
        {"repo": "shop", "member": "orders", "method": "POST", "path": "/orders",
         "handler": "orders::create_order", "file": "orders.py", "line": 1}
    )

    assert frozen["entry_id"] == "root-id"
    assert set(frozen["nodes"]) == {"root-id", "leaf-id"}
    assert frozen["nodes"]["leaf-id"]["source"] == "def save_order():\n    return 1"
    assert frozen["nodes"]["root-id"]["node_key"] == "orders::root-id"
    assert frozen["edges"] == [{
        "source": "root-id", "target": "leaf-id", "call_line": 2,
        "call_site": {"file": "orders.py", "line": 2},
    }]
    assert frozen["api_key"] == "POST|/orders|orders::create_order"
    assert frozen["source_revision"] == "rev"
    assert frozen["truncated"] is False
