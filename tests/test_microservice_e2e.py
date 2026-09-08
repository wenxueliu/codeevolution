"""Real multi-repository CodeGraph fixtures for topology acceptance tests."""

import os
import sqlite3
from pathlib import Path

from codeevolution.analysis.topology.flow import FlowTracer
from codeevolution.analysis.topology.impact import ImpactAnalyzer
from codeevolution.analysis.topology.rules import TopologyRuleSet
from codeevolution.application.advanced_topology_service import AdvancedTopologyService
from codeevolution.application.topology_service import TopologyService
from codeevolution.cross_repo import CrossRepoAnalyzer

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
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, is_exported, is_async, is_static)
           VALUES (?, 'function', ?, ?, 'routes.py', 'python', 1, 100, 1, 0, 0)""",
        (f"{name}-handler", f"{name}_route", f"routes.py::{name}_route_handler"),
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
            "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', ?)",
            (f"{name}-entry-edge-{index}", f"{name}-handler", caller_id, index + 2),
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
            ("entry-mq-edge", "orders-handler", "mq-source"),
            ("entry-rpc-edge", "orders-handler", "rpc-source"),
            ("entry-db-edge", "orders-handler", "db-source"),
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
    gateway_orders = next(
        edge
        for edge in topology.cross_edges
        if edge.source_service == "gateway" and edge.target_service == "orders"
    )
    assert gateway_orders.source_endpoint_path == "/api/gateway"
    assert gateway_orders.source_endpoint_method == "GET"
    assert [node["name"] for node in gateway_orders.call_chain] == [
        "gateway_route",
        "call_orders",
        "requests.get",
    ]
    assert topology.potential_edges[0]["source_endpoint_path"] == "/api/gateway"
    assert topology.potential_edges[0]["kind"] == "external"
    assert len(FlowTracer().trace(topology, "gateway", "/api/gateway")) == 4
    assert FlowTracer().trace(topology, "gateway", "/api/missing") == []
    assert CrossRepoAnalyzer(repositories).analyze() == topology


def test_topology_ignores_http_calls_not_reachable_from_an_entry_endpoint(tmp_path):
    source = _service(tmp_path, "gateway", "/api/gateway", [("orders", "/api/orders")])
    target = _service(tmp_path, "orders", "/api/orders", [])
    database = Path(source["path"]) / ".codegraph" / "codegraph.db"
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM edges WHERE id = 'gateway-entry-edge-0'")
    connection.commit()
    connection.close()

    topology = CrossRepoAnalyzer([source, target]).analyze()

    assert topology.cross_edges == []
    assert topology.dependency_graph == {}
    assert topology.potential_edges == []


def test_hostname_disambiguates_services_that_expose_the_same_path(tmp_path):
    source = _service(tmp_path, "gateway", "/api/gateway", [("orders", "/api/shared")])
    users = _service(tmp_path, "users", "/api/shared", [])
    orders = _service(tmp_path, "orders", "/api/shared", [])

    topology = CrossRepoAnalyzer([source, users, orders]).analyze()

    assert [(edge.source_service, edge.target_service) for edge in topology.cross_edges] == [
        ("gateway", "orders")
    ]
    assert topology.cross_edges[0].evidence["host"] == "orders"


def test_equal_endpoint_matches_remain_candidates_instead_of_false_dependencies(tmp_path):
    source = _service(tmp_path, "gateway", "/api/gateway", [("unknown", "/api/shared")])
    (Path(source["path"]) / "client_0.py").write_text(
        'def call_unknown():\n    return requests.get("/api/shared")\n'
    )
    users = _service(tmp_path, "users", "/api/shared", [])
    orders = _service(tmp_path, "orders", "/api/shared", [])

    topology = CrossRepoAnalyzer([source, users, orders]).analyze()

    assert topology.cross_edges == []
    assert topology.dependency_graph == {}
    assert topology.potential_edges[0]["kind"] == "ambiguous"
    assert {item["service"] for item in topology.potential_edges[0]["candidates"]} == {
        "orders",
        "users",
    }


def test_external_host_does_not_match_an_internal_service_by_path_only(tmp_path):
    source = _service(tmp_path, "gateway", "/api/gateway", [("third-party", "/api/orders")])
    orders = _service(tmp_path, "orders", "/api/orders", [])

    topology = CrossRepoAnalyzer([source, orders]).analyze()

    assert topology.cross_edges == []
    assert topology.dependency_graph == {}
    assert topology.potential_edges[0]["kind"] == "external"
    assert topology.potential_edges[0]["suspected_target"] == "third-party"


def test_multichannel_flow_covers_http_mq_grpc_and_database(tmp_path):
    gateway = _service(tmp_path, "gateway", "/api/gateway", [("orders", "/api/orders")])
    orders = _service(tmp_path, "orders", "/api/orders", [])
    users = _service(tmp_path, "users", "/api/users", [])
    repositories = [gateway, orders, users]
    _advanced_channels(orders, users)

    flow = AdvancedTopologyService.from_repositories(repositories).trace_flow(
        "gateway", "GET /api/gateway"
    )

    assert {"http", "kafka", "grpc", "db"} <= set(flow.channels_used)
    assert any(step.channel == "kafka" and step.to_service == "users" for step in flow.steps)
    assert any(step.channel == "grpc" and step.to_service == "users" for step in flow.steps)
    assert any(step.channel == "db" and step.detail == "table:orders" for step in flow.steps)
    assert all(step.match_rule and step.evidence for step in flow.steps)

    topology = CrossRepoAnalyzer(repositories).analyze()
    assert "users" in topology.dependency_graph["orders"]
    message = topology.message_edges[0]
    assert (message.broker_type, message.channel) == ("kafka", "orders.created")
    assert message.source_endpoint_path == "/api/orders"
    assert [node["name"] for node in message.call_chain] == [
        "orders_route",
        "publish_order",
        "producer.send",
    ]


def test_message_publish_not_reachable_from_entry_does_not_create_dependency(tmp_path):
    orders = _service(tmp_path, "orders", "/api/orders", [])
    users = _service(tmp_path, "users", "/api/users", [])
    _advanced_channels(orders, users)
    connection = sqlite3.connect(Path(orders["path"]) / ".codegraph" / "codegraph.db")
    connection.execute("DELETE FROM edges WHERE id = 'entry-mq-edge'")
    connection.commit()
    connection.close()

    topology = CrossRepoAnalyzer([orders, users]).analyze()

    assert topology.message_edges == []
    assert topology.dependency_graph == {}


def test_redis_pubsub_is_service_dependency_and_cache_is_resource_dependency(tmp_path):
    orders = _service(tmp_path, "orders", "/api/orders", [])
    users = _service(tmp_path, "users", "/api/users", [])
    orders_path = Path(orders["path"])
    (orders_path / "redis_ops.py").write_text(
        'REDIS_URL = "redis://cache:6379/2"\n'
        "def update_order():\n"
        '    redis.set("order:42", "ready")\n'
        '    redis.publish("orders.changed", "42")\n'
    )
    connection = sqlite3.connect(orders_path / ".codegraph" / "codegraph.db")
    connection.executemany(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, is_exported, is_async, is_static)
           VALUES (?, 'function', ?, ?, 'redis_ops.py', 'python', ?, ?, 0, 0, 0)""",
        [
            ("redis-source", "update_order", "redis_ops.py::update_order", 2, 4),
            ("redis-set", "redis.set", "redis.set", 3, 3),
            ("redis-publish", "redis.publish", "redis.publish", 4, 4),
        ],
    )
    connection.executemany(
        "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', ?)",
        [
            ("orders-entry-redis", "orders-handler", "redis-source", 2),
            ("redis-set-edge", "redis-source", "redis-set", 3),
            ("redis-publish-edge", "redis-source", "redis-publish", 4),
        ],
    )
    connection.commit()
    connection.close()

    users_path = Path(users["path"])
    (users_path / "redis_consumer.py").write_text(
        "def consume_changes():\n"
        '    redis.subscribe("orders.changed")\n'
    )
    connection = sqlite3.connect(users_path / ".codegraph" / "codegraph.db")
    connection.executemany(
        """INSERT INTO nodes(id, kind, name, qualified_name, file_path, language,
                   start_line, end_line, is_exported, is_async, is_static)
           VALUES (?, 'function', ?, ?, 'redis_consumer.py', 'python', ?, ?, 0, 0, 0)""",
        [
            ("redis-consumer", "consume_changes", "redis_consumer.py::consume_changes", 1, 2),
            ("redis-subscribe", "redis.subscribe", "redis.subscribe", 2, 2),
        ],
    )
    connection.execute(
        "INSERT INTO edges(id, source, target, kind, line) VALUES (?, ?, ?, 'calls', ?)",
        ("redis-subscribe-edge", "redis-consumer", "redis-subscribe", 2),
    )
    connection.commit()
    connection.close()

    topology = CrossRepoAnalyzer([orders, users]).analyze()

    redis_message = next(e for e in topology.message_edges if e.broker_type == "redis_pubsub")
    assert redis_message.source_service == "orders"
    assert redis_message.target_service == "users"
    assert redis_message.channel == "orders.changed"
    assert topology.dependency_graph == {"orders": ["users"]}
    cache_edge = next(e for e in topology.resource_edges if e.operation == "SET")
    assert cache_edge.resource_id == "redis://cache:6379/2"
    assert cache_edge.resource_key == "order:42"
    assert cache_edge.source_endpoint_path == "/api/orders"
    assert topology.resource_dependency_graph == {
        "orders": ["redis://cache:6379/2"],
        "users": ["redis:default"],
    }

    impact = ImpactAnalyzer().analyze(topology, "orders")
    assert impact["affected_message_edges"][0]["channel"] == "orders.changed"
    assert any(edge["operation"] == "SET" for edge in impact["affected_resource_edges"])


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


def test_incremental_topology_reuses_unchanged_service_analysis(tmp_path):
    repositories = [
        _service(tmp_path, "gateway", "/gateway", [("orders", "/orders")]),
        _service(tmp_path, "orders", "/orders", [("users", "/users")]),
        _service(tmp_path, "users", "/users", []),
    ]
    service = TopologyService.from_repositories(repositories)

    first = service.get_or_build(force=True)
    assert service.analyzer.cache_stats == {"hits": 0, "misses": 3}
    second = service.get_or_build(force=True)
    assert second == first
    assert service.analyzer.cache_stats == {"hits": 3, "misses": 0}

    changed_database = Path(repositories[1]["path"]) / ".codegraph" / "codegraph.db"
    stat = changed_database.stat()
    os.utime(changed_database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = service.get_or_build(force=True)
    assert third == first
    assert service.analyzer.cache_stats == {"hits": 2, "misses": 1}


def test_grouped_repositories_are_merged_into_one_logical_service(tmp_path):
    backend = _service(tmp_path, "mall-backend", "/api/products", [])
    frontend = _service(tmp_path, "mall-admin-web", "/admin", [])
    grouped = {
        "name": "mall",
        "path": backend["path"],
        "repositories": [backend, frontend],
    }

    topology = TopologyService.from_repositories([grouped]).get_or_build(force=True)

    assert len(topology.services) == 1
    assert topology.services[0].name == "mall"
    assert {api["path"] for api in topology.services[0].apis} == {"/api/products", "/admin"}
