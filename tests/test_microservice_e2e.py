"""Real multi-repository CodeGraph fixtures for topology acceptance tests."""

import sqlite3
from pathlib import Path

from codehistory.application.advanced_topology_service import AdvancedTopologyService
from codehistory.application.topology_service import TopologyService
from codehistory.cross_repo import CrossRepoAnalyzer
from codehistory.analysis.topology.rules import TopologyRuleSet


SCHEMA = """
CREATE TABLE nodes (
    id TEXT, kind TEXT, name TEXT, qualified_name TEXT, file_path TEXT,
    language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    visibility TEXT, is_exported INTEGER, is_async INTEGER, is_static INTEGER,
    decorators TEXT
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


def _service(root: Path, name: str, route: str, outbound: list[tuple[str, str]]) -> dict:
    path = root / name
    database = path / ".codegraph" / "codegraph.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO nodes(id, kind, name, qualified_name, file_path, language, start_line) "
        "VALUES (?, 'route', ?, ?, 'routes.py', 'python', 1)",
        (f"{name}-route", f"GET {route}", f"routes.py::{name}_route"),
    )
    connection.execute(
        "INSERT INTO files(path, language, size, node_count) VALUES ('routes.py', 'python', 1, 1)"
    )
    for index, (target, target_route) in enumerate(outbound):
        file_path = f"client_{index}.py"
        qualified_name = f"{file_path}::call_{target}"
        (path / file_path).write_text(
            f'def call_{target}():\n    return requests.get("http://{target}{target_route}")\n'
        )
        caller_id, callee_id = f"{name}-caller-{index}", f"{name}-callee-{index}"
        connection.execute(
            """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                       start_line, end_line, is_exported, is_async, is_static)
               VALUES (?, 'function', ?, ?, ?, 'python', 1, 2, 0, 0, 0)""",
            (caller_id, f"call_{target}", qualified_name, file_path),
        )
        connection.execute(
            """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                       start_line, end_line, is_exported, is_async, is_static)
               VALUES (?, 'function', 'requests.get', 'requests.get', ?, 'python', 1, 1, 0, 0, 0)""",
            (callee_id, file_path),
        )
        connection.execute(
            "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', 2)",
            (f"{name}-edge-{index}", caller_id, callee_id),
        )
        connection.execute(
            "INSERT INTO files(path, language, size, node_count) VALUES (?, 'python', 1, 2)",
            (file_path,),
        )
    connection.commit()
    connection.close()
    return {"name": name, "path": str(path)}


def _advanced_channels(orders: dict, users: dict) -> None:
    orders_path = Path(orders["path"])
    database = orders_path / ".codegraph" / "codegraph.db"
    connection = sqlite3.connect(database)
    (orders_path / "events.py").write_text(
        'def publish_order():\n    producer.send("orders.created")\n'
    )
    nodes = [
        ("mq-source", "publish_order", "events.py::publish_order", "events.py", ""),
        ("mq-target", "producer.send", "producer.send", "events.py", ""),
        ("rpc-source", "load_user", "rpc.py::load_user", "rpc.py", ""),
        ("rpc-target", "usersRpcClient", "usersRpcClient", "rpc.py", ""),
        ("db-source", "save_order", "db.py::save_order", "db.py", ""),
        ("db-target", "execute", "db.execute", "db.py", "INSERT INTO orders(id) VALUES (?)"),
    ]
    connection.executemany(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, signature, is_exported, is_async, is_static)
           VALUES (?, 'function', ?, ?, ?, 'python', 1, 2, ?, 0, 0, 0)""",
        nodes,
    )
    connection.executemany(
        "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', 2)",
        [
            ("mq-edge", "mq-source", "mq-target"),
            ("rpc-edge", "rpc-source", "rpc-target"),
            ("db-edge", "db-source", "db-target"),
        ],
    )
    connection.commit()
    connection.close()

    users_path = Path(users["path"])
    (users_path / "consumer.py").write_text(
        '@KafkaListener("orders.created")\ndef handle_order():\n    pass\n'
    )
    connection = sqlite3.connect(users_path / ".codegraph" / "codegraph.db")
    connection.execute(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, decorators, is_exported, is_async, is_static)
           VALUES ('mq-consumer', 'function', 'KafkaListener', 'consumer.py::handle_order',
                   'consumer.py', 'python', 1, 3, '["@KafkaListener"]', 0, 0, 0)"""
    )
    connection.commit()
    connection.close()


def test_three_service_http_topology_cycle_diamond_unknown_and_facade_parity(tmp_path):
    repositories = [
        _service(
            tmp_path,
            "gateway",
            "/api/gateway",
            [("orders", "/api/orders"), ("users", "/api/users"), ("external", "/api/audit")],
        ),
        _service(tmp_path, "orders", "/api/orders", [("users", "/api/users")]),
        _service(tmp_path, "users", "/api/users", [("gateway", "/api/gateway")]),
    ]

    topology = TopologyService.from_repositories(repositories).get_or_build(force=True)

    assert topology.dependency_graph == {
        "gateway": ["orders", "users"],
        "orders": ["users"],
        "users": ["gateway"],
    }
    assert len(topology.cross_edges) == 4
    assert topology.potential_edges[0]["suspected_target"] == "external"
    assert CrossRepoAnalyzer(repositories).analyze() == topology


def test_multichannel_flow_covers_http_mq_grpc_and_database(tmp_path):
    gateway = _service(tmp_path, "gateway", "/api/gateway", [("orders", "/api/orders")])
    orders = _service(tmp_path, "orders", "/api/orders", [])
    users = _service(tmp_path, "users", "/api/users", [])
    repositories = [gateway, orders, users]
    _advanced_channels(orders, users)

    flow = AdvancedTopologyService.from_repositories(repositories).trace_flow("gateway")

    assert {"http", "kafka", "grpc", "db"} <= set(flow.channels_used)
    assert any(step.channel == "kafka" and step.to_service == "users" for step in flow.steps)
    assert any(step.channel == "grpc" and step.to_service == "users" for step in flow.steps)
    assert any(step.channel == "db" and step.detail == "table:orders" for step in flow.steps)
    assert all(step.match_rule and step.evidence for step in flow.steps)


def test_custom_topology_rules_drive_detection_and_evidence_version(tmp_path):
    source = _service(tmp_path, "gateway", "/api/gateway", [("orders", "/api/orders")])
    target = _service(tmp_path, "orders", "/api/orders", [])
    connection = sqlite3.connect(Path(source["path"]) / ".codegraph" / "codegraph.db")
    connection.execute(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language, start_line)
           VALUES ('custom-db', 'import', 'acme_database', 'acme_database', 'db.py', 'python', 1)"""
    )
    connection.commit()
    connection.close()
    rules = TopologyRuleSet(
        version="acme-v2",
        http_client_callers={"python": [("requests.get", "GET")]},
        database_patterns={"acme-db": ("acme_database",)},
        message_queue_patterns={"acme-mq": ("acme_broker",)},
    )

    topology = CrossRepoAnalyzer([source, target], rules).analyze()

    assert topology.services[0].db_type == "acme-db"
    assert topology.cross_edges[0].rule_version == "acme-v2"
