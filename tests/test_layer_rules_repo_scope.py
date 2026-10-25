import json
import sqlite3

from codeevolution.application.repository_attempt_worker import (
    _apply_declared_layers,
    _declared_layers_digest,
)
from codeevolution.analysis.communication.rules import TIER1_RULES_DIGEST


def _codegraph(path, files):
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
    for index, path_value in enumerate(files):
        connection.execute(
            "INSERT INTO nodes (id, kind, name, file_path) VALUES (?, 'function', ?, ?)",
            (f"n{index}", f"fn{index}", path_value),
        )
        connection.execute("INSERT INTO files (path) VALUES (?)", (path_value,))
    connection.execute(
        "INSERT INTO edges (id, source, target, kind, line) VALUES ('e1', 'n0', 'n1', 'calls', 12)"
    )
    connection.commit()
    connection.close()


def _declaration(**rules):
    return {
        "version": "layers/v1",
        "layers": {"presentation": ["**/web/**"], "domain": ["**/domain/**"]},
        "rules": {"strict": True, "allow": rules.get("allow", []), "ignore": []},
    }


def test_declared_config_replaces_layer_violations(tmp_path):
    repository = tmp_path / "repo"
    (repository / ".codeevolution").mkdir(parents=True)
    (repository / ".codeevolution" / "layers.json").write_text(
        json.dumps(_declaration()), encoding="utf-8"
    )
    graph = tmp_path / "codegraph.db"
    _codegraph(graph, ["app/web/orders.py", "app/domain/order.py"])

    facts = _apply_declared_layers(
        {"layer_violations": {"violation_count": 0, "violations": []}}, graph, repository
    )

    assert facts["layer_violations"]["violation_count"] == 1
    violation = facts["layer_violations"]["violations"][0]
    assert violation["source_layer"] == "presentation"
    assert violation["target_layer"] == "domain"
    assert violation["source_file"] == "app/web/orders.py"


def test_allowed_edge_is_not_a_violation(tmp_path):
    repository = tmp_path / "repo"
    (repository / ".codeevolution").mkdir(parents=True)
    (repository / ".codeevolution" / "layers.json").write_text(
        json.dumps(_declaration(allow=[["presentation", "domain"]])), encoding="utf-8"
    )
    graph = tmp_path / "codegraph.db"
    _codegraph(graph, ["app/web/orders.py", "app/domain/order.py"])

    facts = _apply_declared_layers(
        {"layer_violations": {"violation_count": 3, "violations": [{}]}}, graph, repository
    )

    assert facts["layer_violations"] == {"violation_count": 0, "violations": []}


def test_undeclared_repository_keeps_builtin_facts(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    graph = tmp_path / "codegraph.db"
    _codegraph(graph, ["app/web/orders.py", "app/domain/order.py"])
    facts = {"layer_violations": {"violation_count": 7, "violations": [{}]}}

    assert _apply_declared_layers(facts, graph, repository) is facts


def test_invalid_declaration_is_ignored(tmp_path):
    repository = tmp_path / "repo"
    (repository / ".codeevolution").mkdir(parents=True)
    (repository / ".codeevolution" / "layers.json").write_text("{not json", encoding="utf-8")
    graph = tmp_path / "codegraph.db"
    _codegraph(graph, ["app/web/orders.py", "app/domain/order.py"])
    facts = {"layer_violations": {"violation_count": 2, "violations": [{}]}}

    assert _apply_declared_layers(facts, graph, repository)["layer_violations"]["violation_count"] == 2


def test_declaration_changes_the_rules_digest(tmp_path):
    repository = tmp_path / "repo"
    (repository / ".codeevolution").mkdir(parents=True)
    config = repository / ".codeevolution" / "layers.json"
    assert _declared_layers_digest(repository) == TIER1_RULES_DIGEST

    config.write_text(json.dumps(_declaration()), encoding="utf-8")
    first = _declared_layers_digest(repository)
    config.write_text(json.dumps(_declaration(allow=[["presentation", "domain"]])), encoding="utf-8")
    second = _declared_layers_digest(repository)

    assert first != TIER1_RULES_DIGEST
    assert first != second
