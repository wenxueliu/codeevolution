"""P2 — Advanced multi-service analysis.

  1. Enhanced Flow Tracer — HTTP + MQ (Kafka/RabbitMQ/NATS) + gRPC + DB,
     with visualized full multi-hop chains across all channels.

  2. Cross-Service Entity Alignment — detect same business concept with
     different names across services (Order ↔ Transaction ↔ Stock).
     Uses naming similarity + optional LLM verification.

Reads from each service's .codegraph/codegraph.db via CodeGraphReader.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .infrastructure.codegraph_sqlite import read_rows


# ── MQ producer/consumer patterns ──────────────────────────────────────

MQ_PRODUCER_PATTERNS = [
    ("kafka", [
        "KafkaProducer.send", "kafka.produce", "producer.send",
        "kafka-go.Writer.WriteMessages", "kafka.SendMessage",
        "confluent_kafka.Producer.produce",
    ]),
    ("rabbitmq", [
        "basic_publish", "channel.publish", "exchange.publish",
        "pika.BasicProperties", "amqp.Channel.publish",
        "amqplib.Channel.publish",
    ]),
    ("nats", [
        "nats.publish", "stan.publish", "js.publish",
        "nats.Conn.Publish", "nats.go",
    ]),
    ("celery", [
        "celery.task.send_task", "apply_async", "delay(",
        "task.delay", "task.apply_async",
    ]),
    ("sqs", [
        "SQS.send_message", "sqs.sendMessage", "sqs-client.send",
    ]),
    ("pubsub", [
        "pubsub.publish", "topic.publish", "Publisher.publish",
    ]),
]

MQ_CONSUMER_PATTERNS = [
    ("kafka", [
        "KafkaConsumer", "kafka.consume", "consumer.subscribe",
        "kafka-go.Reader.FetchMessage", "kafka.ReadMessage",
        "confluent_kafka.Consumer.consume",
        "@KafkaListener", "kafka_listener",
    ]),
    ("rabbitmq", [
        "basic_consume", "channel.consume", "queue.consume",
        "pika.BlockingConnection.consume",
        "@RabbitListener", "rabbitmq_listener",
    ]),
    ("nats", [
        "nats.subscribe", "stan.subscribe", "js.subscribe",
        "nats.Conn.Subscribe",
    ]),
    ("celery", [
        "@celery.task", "@shared_task", "celery_app.task",
        "@app.task", "@task(",
    ]),
    ("sqs", [
        "SQS.receive_message", "sqs.receiveMessage",
        "@SqsListener", "sqs_listener",
    ]),
    ("pubsub", [
        "pubsub.subscribe", "subscription.create",
        "@PubSubListener", "pubsub_listener",
    ]),
]

# gRPC / RPC patterns
RPC_PATTERNS = [
    "grpc.", "grpc::", "grpc-", "_pb2", "_grpc.", "Stub(",
    "Channel(", "GrpcClient", "RpcClient",
    "ServiceClient", "BlockingStub", "FutureStub",
    "Thrift.", "TBinaryProtocol", "TCompactProtocol",
]

# ── Output types ───────────────────────────────────────────────────────

@dataclass
class FlowStep:
    """A single step in an end-to-end flow."""
    depth: int
    channel: str           # "http" | "kafka" | "rabbitmq" | "grpc" | "db" | "internal"
    from_service: str
    from_function: str
    to_service: str
    to_function: str
    detail: str            # URL, topic name, SQL table, etc.


@dataclass
class FlowDiagram:
    """Complete end-to-end flow trace with all communication channels."""
    entry_service: str
    entry_api: str
    steps: list[FlowStep]
    services_involved: list[str]
    total_cross_service_calls: int
    channels_used: list[str]


@dataclass
class EntityMapping:
    """Two entities from different services that represent the same concept."""
    source_service: str
    source_entity: str
    target_service: str
    target_entity: str
    confidence: float            # 0-1: similarity score
    relationship: str            # "same" | "subset" | "related" | "wrapper"
    description: str = ""        # LLM-generated explanation


@dataclass
class CrossServiceEntities:
    """All cross-service entity mappings."""
    services: list[str]
    entities_per_service: dict[str, list[str]]  # service → [entity_names]
    mappings: list[EntityMapping]
    unmapped_entities: dict[str, list[str]]     # service → entities without matches


# ── P2 Analyzer ────────────────────────────────────────────────────────

class P2Analyzer:
    """Advanced multi-service analysis.

    Usage:
        analyzer = P2Analyzer([
            {"name": "order-svc", "path": "/repos/order-svc"},
            {"name": "user-svc",  "path": "/repos/user-svc"},
        ])
        flow = analyzer.trace_full_flow("order-svc", "POST /api/orders")
        entities = analyzer.align_entities()
    """

    def __init__(self, repos: list[dict]):
        self.repos = repos

    def _cg_db(self, repo_path: str) -> str:
        return str(Path(repo_path) / ".codegraph" / "codegraph.db")

    def _query(self, db_path: str, sql: str, params=None) -> list[dict]:
        return read_rows(db_path, sql, params)

    # ── 1. Enhanced Flow Tracer ─────────────────────────────────────────

    def trace_full_flow(
        self, entry_service: str, entry_api_pattern: str = "", max_depth: int = 6
    ) -> FlowDiagram:
        """Trace complete end-to-end flow across all communication channels.

        Traces: HTTP calls + MQ (publish → topic → consume) + gRPC + DB access.
        """
        steps: list[FlowStep] = []
        services_involved: set[str] = {entry_service}
        channels_used: set[str] = set()

        # Phase A: collect all channels from each service
        http_edges = self._collect_http_edges()
        mq_pub, mq_sub = self._collect_mq_channels()
        rpc_edges = self._collect_rpc_edges()

        # Phase B: BFS through all channels, starting from entry
        visited_pairs: set[tuple[str, str, str]] = set()
        queue: list[tuple[str, str, int]] = [(entry_service, entry_api_pattern, 0)]

        while queue:
            svc, trigger, depth = queue.pop(0)
            if depth > max_depth:
                continue

            # B1: follow HTTP calls from this service
            for e in http_edges.get(svc, []):
                edge_key = (svc, e["target_service"], e["url_pattern"])
                if edge_key in visited_pairs:
                    continue
                visited_pairs.add(edge_key)
                channels_used.add("http")

                step = FlowStep(
                    depth=depth,
                    channel="http",
                    from_service=svc,
                    from_function=e["source_function"],
                    to_service=e["target_service"],
                    to_function=e.get("target_function", ""),
                    detail=f"{e['http_method']} {e['url_pattern']}",
                )
                steps.append(step)
                services_involved.add(e["target_service"])
                services_involved.add(svc)
                queue.append((e["target_service"], "", depth + 1))

            # B2: follow MQ publish → consume chains
            for pub in mq_pub.get(svc, []):
                topic = pub.get("topic", "")
                mq_type = pub["mq_type"]
                edge_key = (svc, f"mq:{mq_type}:{topic}", "publish")
                if edge_key in visited_pairs:
                    continue
                visited_pairs.add(edge_key)
                channels_used.add(mq_type)

                # Find consumers of this topic
                matched_consumer = False
                for consumer_svc, subs in mq_sub.items():
                    if consumer_svc == svc:
                        continue
                    for sub in subs:
                        if sub["mq_type"] == mq_type and self._topics_match(topic, sub.get("topic", "")):
                            step_pub = FlowStep(
                                depth=depth,
                                channel=mq_type,
                                from_service=svc,
                                from_function=pub["function"],
                                to_service=f"{{{mq_type}:{topic}}}",
                                to_function="",
                                detail=f"publish → topic:{topic}",
                            )
                            steps.append(step_pub)
                            channels_used.add(mq_type)

                            step_sub = FlowStep(
                                depth=depth + 1,
                                channel=mq_type,
                                from_service=f"{{{mq_type}:{topic}}}",
                                from_function="",
                                to_service=consumer_svc,
                                to_function=sub["function"],
                                detail=f"topic:{topic} → consume",
                            )
                            steps.append(step_sub)
                            services_involved.add(svc)
                            services_involved.add(consumer_svc)
                            queue.append((consumer_svc, topic, depth + 2))
                            matched_consumer = True

                if not matched_consumer:
                    # No consumer found — still show the publish
                    step = FlowStep(
                        depth=depth,
                        channel=mq_type,
                        from_service=svc,
                        from_function=pub["function"],
                        to_service=f"{{{mq_type}:{topic}}}",
                        to_function="",
                        detail=f"publish → topic:{topic} (no consumer found)",
                    )
                    steps.append(step)
                    channels_used.add(mq_type)

            # B3: follow gRPC calls
            for rpc in rpc_edges.get(svc, []):
                edge_key = (svc, rpc["target_service"], rpc.get("service_method", ""))
                if edge_key in visited_pairs:
                    continue
                visited_pairs.add(edge_key)
                channels_used.add("grpc")

                step = FlowStep(
                    depth=depth,
                    channel="grpc",
                    from_service=svc,
                    from_function=rpc["source_function"],
                    to_service=rpc["target_service"],
                    to_function=rpc.get("target_function", ""),
                    detail=rpc.get("service_method", "gRPC call"),
                )
                steps.append(step)
                services_involved.add(rpc["target_service"])
                services_involved.add(svc)
                queue.append((rpc["target_service"], "", depth + 1))

        return FlowDiagram(
            entry_service=entry_service,
            entry_api=entry_api_pattern,
            steps=sorted(steps, key=lambda s: (s.depth, s.from_service)),
            services_involved=sorted(services_involved),
            total_cross_service_calls=len(steps),
            channels_used=sorted(channels_used),
        )

    @staticmethod
    def _topics_match(a: str, b: str) -> bool:
        """Check if two MQ topic/queue names match."""
        if not a or not b:
            return True  # unknown topics match broadly
        a_clean = a.lower().strip().replace("-", "").replace("_", "")
        b_clean = b.lower().strip().replace("-", "").replace("_", "")
        return a_clean == b_clean or a_clean in b_clean or b_clean in a_clean

    # ── Channel collectors ──────────────────────────────────────────────

    def _collect_http_edges(self) -> dict[str, list[dict]]:
        """Collect HTTP cross-service edges from all repos."""
        from .cross_repo import CrossRepoAnalyzer
        analyzer = CrossRepoAnalyzer(self.repos)
        topology = analyzer.analyze()

        edges: dict[str, list[dict]] = defaultdict(list)
        for e in topology.cross_edges:
            edges[e.source_service].append({
                "source_function": e.source_function,
                "target_service": e.target_service,
                "target_function": e.target_function,
                "http_method": e.http_method,
                "url_pattern": e.url_pattern,
            })
        return dict(edges)

    def _collect_mq_channels(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """Collect MQ producers and consumers from all services."""
        producers: dict[str, list[dict]] = defaultdict(list)
        consumers: dict[str, list[dict]] = defaultdict(list)

        for repo in self.repos:
            db = self._cg_db(repo["path"])
            if not Path(db).exists():
                continue
            svc_name = repo["name"]

            # Find producers
            for mq_type, patterns in MQ_PRODUCER_PATTERNS:
                for pat in patterns:
                    rows = self._query(db, """
                        SELECT n1.qualified_name AS caller, n1.name,
                               n1.file_path, n1.start_line,
                               e.line AS call_line,
                               n2.name AS callee_name
                        FROM edges e
                        JOIN nodes n1 ON n1.id = e.source
                        JOIN nodes n2 ON n2.id = e.target
                        WHERE e.kind = 'calls' AND n2.name LIKE ?
                    """, [f"%{pat}%"])
                    for r in rows:
                        # Extract topic from source code near the call
                        topic = self._extract_topic_from_source(
                            db, r["caller"], r.get("file_path"),
                            r.get("call_line") or r.get("start_line")
                        )
                        if not topic:
                            # Fallback: look for topic constants in the function
                            topic = self._extract_topic_from_function(db, r["caller"])
                        producers[svc_name].append({
                            "mq_type": mq_type,
                            "function": r["caller"],
                            "topic": topic or pat.split(".")[-1],
                        })

            # Find consumers
            for mq_type, patterns in MQ_CONSUMER_PATTERNS:
                for pat in patterns:
                    rows = self._query(db, """
                        SELECT n1.qualified_name, n1.name, n1.decorators,
                               n1.file_path, n1.start_line
                        FROM nodes n1
                        WHERE n1.kind IN ('function', 'method')
                          AND (n1.name LIKE ? OR n1.decorators LIKE ?)
                    """, [f"%{pat}%", f"%{pat}%"])
                    for r in rows:
                        # Extract topic from decorator or function context
                        topic = self._extract_topic_from_decorator(r.get("decorators") or "")
                        if not topic:
                            topic = self._extract_topic_from_source(
                                db, r["qualified_name"],
                                r.get("file_path"), r.get("start_line")
                            )
                        if not topic:
                            topic = self._guess_topic(r["name"], mq_type)
                        consumers[svc_name].append({
                            "mq_type": mq_type,
                            "function": r["qualified_name"],
                            "topic": topic,
                        })

        return dict(producers), dict(consumers)

    def _extract_topic_from_source(
        self, db_path: str, caller_qname: str,
        file_path: str | None, line: int | None
    ) -> str | None:
        """Read source code around a producer/consumer call to extract topic/queue name.

        Handles:
          - f-strings: f"order.created" / f"{TOPIC}.created"
          - plain strings: "order.created"
          - template literals: `order.${event}`
          - variable references: topic = "order.created"
          - decorator args: @KafkaListener(topics=["order.created"])
        """
        if not file_path or not line or line < 1:
            return None

        repo_root = str(Path(db_path).parent.parent)
        source_path = str(Path(repo_root) / file_path)
        try:
            with open(source_path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (OSError, FileNotFoundError):
            return None

        if line > len(lines):
            return None

        start = max(0, line - 6)
        end = min(len(lines), line + 3)
        context = "".join(lines[start:end])

        # Pattern 1: plain topic string — "order.created" or 'payment.completed'
        m = re.search(r'''['\"]([\w.-]+\.[\w.-]+(?:[\w.-]*))['\"]''', context)
        if m:
            candidate = m.group(1)
            # Filter out things that are clearly not topic names
            if not candidate.startswith(("http", "https", "//", "./", "../")) and "." in candidate:
                return candidate

        # Pattern 2: f-string topic — f"{prefix}.created" or f"order.{event}"
        m = re.search(r'''f['\"][{]?\w+[}]?\.[\w.{}]+['\"]''', context)
        if m:
            # Normalize: keep the template-ish format
            raw = m.group(0).strip("f").strip("\"'")
            raw = re.sub(r'\{[^}]+\}', '*', raw)  # replace {var} with *
            return raw

        # Pattern 3: topic from decorator: @KafkaListener(topics=["order.created"])
        m = re.search(r'''(?:topics?|queues?|destinations?)\s*=\s*\[?\s*['\"]([^'\"]+)['\"]''', context)
        if m:
            return m.group(1)

        # Pattern 4: variable assignment before the call: TOPIC = "order.created"
        m = re.search(
            r'''(?:TOPIC|QUEUE|EXCHANGE|ROUTING_KEY)\s*=\s*['\"]([\w.-]+)['\"]''',
            context, re.IGNORECASE
        )
        if m:
            return m.group(1)

        # Pattern 5: routing key / binding key
        m = re.search(
            r'''(?:routing_key|binding_key|routingKey|bindingKey)\s*=\s*['\"]([\w.-]+)['\"]''',
            context, re.IGNORECASE
        )
        if m:
            return m.group(1)

        return None

    def _extract_topic_from_function(self, db_path: str, caller_qname: str) -> str | None:
        """Extract topic/queue name from constants near a function."""
        parts = caller_qname.split("::")
        if len(parts) < 2:
            return None
        file_path = parts[0]
        func_name = parts[-1].split(".")[-1]

        rows = self._query(db_path, """
            SELECT name FROM nodes
            WHERE file_path = ? AND kind IN ('variable', 'constant')
              AND (name LIKE '%topic%' OR name LIKE '%queue%'
                   OR name LIKE '%TOPIC%' OR name LIKE '%QUEUE%'
                   OR name LIKE '%exchange%' OR name LIKE '%EXCHANGE%')
            LIMIT 10
        """, [file_path])
        for r in rows:
            # Return the variable value if it looks like a topic name
            name = r["name"]
            # Extract string value from variable assignment
            m = re.search(rf'{re.escape(name)}\s*=\s*["\']([^"\']+)["\']', name)
            if not m:
                return name.strip('"\'').split("=")[-1].strip().strip('"\'')
        return None

    @staticmethod
    def _extract_topic_from_decorator(decorators_raw: str) -> str | None:
        """Extract topic/queue name from decorator arguments."""
        if not decorators_raw:
            return None
        try:
            decos = json.loads(decorators_raw) if isinstance(decorators_raw, str) else decorators_raw
        except (json.JSONDecodeError, TypeError):
            decos = [decorators_raw]
        for d in decos:
            m = re.search(r'''['\"]([^'\"]+(?:topic|queue|exchange)[^'\"]*)['\"]''', str(d), re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _guess_topic(func_name: str, mq_type: str) -> str:
        """Guess topic name from function naming convention."""
        for prefix in ("handle_", "process_", "consume_", "listen_", "on_"):
            if func_name.startswith(prefix):
                return func_name[len(prefix):]
        return func_name

    def _collect_rpc_edges(self) -> dict[str, list[dict]]:
        """Collect gRPC/Thrift call edges."""
        edges: dict[str, list[dict]] = defaultdict(list)

        for repo in self.repos:
            db = self._cg_db(repo["path"])
            if not Path(db).exists():
                continue
            svc_name = repo["name"]

            for pat in RPC_PATTERNS:
                rows = self._query(db, """
                    SELECT n1.qualified_name AS caller, n2.name AS callee_name
                    FROM edges e
                    JOIN nodes n1 ON n1.id = e.source
                    JOIN nodes n2 ON n2.id = e.target
                    WHERE e.kind = 'calls' AND n2.name LIKE ?
                    LIMIT 20
                """, [f"%{pat}%"])
                for r in rows:
                    # Guess target service from callee name
                    callee = r["callee_name"].lower()
                    target = "unknown"
                    for other in self.repos:
                        if other["name"] != svc_name and other["name"].replace("-", "") in callee.replace("_", "").replace("-", ""):
                            target = other["name"]
                            break

                    edges[svc_name].append({
                        "source_function": r["caller"],
                        "target_service": target,
                        "target_function": r["callee_name"],
                        "service_method": r["callee_name"],
                    })

        return dict(edges)

    # ── 2. Cross-Service Entity Alignment ───────────────────────────────

    def align_entities(self, use_llm: bool = False) -> CrossServiceEntities:
        """Align business entities across services.

        Args:
            use_llm: If True, use LLM to verify and explain mappings.
                     Requires OPENAI_API_KEY or ANTHROPIC_API_KEY.
        """
        # Collect entities from each service
        entities_per_service: dict[str, list[dict]] = {}
        for repo in self.repos:
            db = self._cg_db(repo["path"])
            if not Path(db).exists():
                continue

            # Get classes, structs, interfaces, enums
            rows = self._query(db, """
                SELECT name, kind, qualified_name, file_path
                FROM nodes
                WHERE kind IN ('class', 'struct', 'interface', 'enum', 'type_alias')
                  AND name NOT LIKE 'test%' AND name NOT LIKE 'Test%'
                  AND name NOT LIKE '%Test' AND name NOT LIKE '%Tests'
                  AND file_path NOT LIKE '%test%' AND file_path NOT LIKE '%Test%'
                ORDER BY name
            """)
            entities_per_service[repo["name"]] = [
                {"name": r["name"], "kind": r["kind"],
                 "qualified_name": r["qualified_name"]}
                for r in rows
            ]

        # Find cross-service mappings by naming similarity
        mappings: list[EntityMapping] = []
        unmapped: dict[str, list[str]] = defaultdict(list)

        svc_names = list(entities_per_service.keys())
        for i in range(len(svc_names)):
            for j in range(i + 1, len(svc_names)):
                svc_a, svc_b = svc_names[i], svc_names[j]
                ents_a = entities_per_service[svc_a]
                ents_b = entities_per_service[svc_b]

                matched_b = set()
                for ea in ents_a:
                    best_score = 0.0
                    best_entity = None
                    for eb in ents_b:
                        if eb["name"] in matched_b:
                            continue
                        score = self._entity_similarity(ea["name"], eb["name"])
                        if score > best_score and score >= 0.6:
                            best_score = score
                            best_entity = eb

                    if best_entity:
                        matched_b.add(best_entity["name"])
                        mappings.append(EntityMapping(
                            source_service=svc_a,
                            source_entity=ea["name"],
                            target_service=svc_b,
                            target_entity=best_entity["name"],
                            confidence=round(best_score, 2),
                            relationship=self._infer_relationship(
                                ea["name"], best_entity["name"], best_score
                            ),
                        ))
                    else:
                        unmapped[svc_a].append(ea["name"])

                for eb in ents_b:
                    if eb["name"] not in matched_b:
                        unmapped[svc_b].append(eb["name"])

        # LLM verification if requested
        if use_llm and mappings:
            mappings = self._llm_verify_mappings(mappings)

        return CrossServiceEntities(
            services=svc_names,
            entities_per_service={
                svc: [e["name"] for e in ents]
                for svc, ents in entities_per_service.items()
            },
            mappings=mappings,
            unmapped_entities=dict(unmapped),
        )

    @staticmethod
    def _entity_similarity(name_a: str, name_b: str) -> float:
        """Compute similarity between two entity names.

        Uses a combination of:
          - Exact match bonus
          - Acronym expansion (UserAcct ↔ UserAccount)
          - Word overlap (Jaccard on split words)
          - Edit distance for short names
        """
        if name_a == name_b:
            return 1.0

        a, b = name_a.lower(), name_b.lower()

        # CamelCase/underscore splitting — must be done on the ORIGINAL case
        def split_name(original: str) -> set[str]:
            # Split by underscore first
            parts = original.replace("_", " ").split()
            words = set()
            for p in parts:
                # Split camelCase: "OrderService" → ["Order", "Service"]
                camel_parts = re.findall(r'[A-Z]?[a-z0-9]+', p)
                for cp in camel_parts:
                    if cp:
                        words.add(cp.lower())
            # If camelCase didn't split anything, use the whole lowercase word
            return words or {original.lower()}

        words_a = split_name(name_a)
        words_b = split_name(name_b)

        if not words_a or not words_b:
            return 0.0

        # Jaccard similarity on word sets
        intersection = words_a & words_b
        union = words_a | words_b
        jaccard = len(intersection) / len(union) if union else 0.0

        # One-way containment bonus
        if words_a.issubset(words_b) or words_b.issubset(words_a):
            jaccard = max(jaccard, 0.7)

        # Suffix similarity — use ORIGINAL case for camelCase matching
        common_suffixes = {
            "service": {"svc", "service"},
            "repository": {"repo", "repository"},
            "controller": {"ctrl", "controller"},
            "manager": {"mgr", "manager"},
            "configuration": {"config", "configuration"},
            "request": {"req", "request"},
            "response": {"resp", "response"},
            "message": {"msg", "message"},
            "metadata": {"meta", "metadata"},
            "application": {"app", "application"},
        }
        def strip_suffix(name: str, suffixes: set[str]) -> str:
            for s in sorted(suffixes, key=len, reverse=True):
                suf_lower = s.lower()
                suf_title = s[0].upper() + s[1:] if s else ""
                # snake_case: _suffix
                if name.endswith(f"_{suf_lower}"):
                    return name[:-len(f"_{suf_lower}")]
                # TitleCase: Suffix
                if suf_title and name.endswith(suf_title):
                    stem = name[:-len(suf_title)]
                    if stem:
                        return stem
            return name

        for suffix, variants in common_suffixes.items():
            a_stem = strip_suffix(name_a, variants)
            b_stem = strip_suffix(name_b, variants)
            if a_stem != name_a and b_stem != name_b and a_stem.lower() == b_stem.lower():
                jaccard = max(jaccard, 0.85)
            elif a_stem != name_a and a_stem.lower() in name_b.lower():
                jaccard = max(jaccard, 0.75)
            elif b_stem != name_b and b_stem.lower() in name_a.lower():
                jaccard = max(jaccard, 0.75)

        return jaccard

    @staticmethod
    def _infer_relationship(name_a: str, name_b: str, score: float) -> str:
        """Infer the relationship type between two entity names."""
        if name_a == name_b:
            return "same"
        a, b = name_a.lower(), name_b.lower()
        if score >= 0.9:
            return "same"
        if a in b or b in a:
            return "subset"
        if score >= 0.7:
            return "related"
        return "related"

    def _llm_verify_mappings(self, mappings: list[EntityMapping]) -> list[EntityMapping]:
        """Use LLM to verify and explain entity mappings."""
        from .llm import is_available as llm_ready, _call_llm, _parse_json

        if not llm_ready():
            return mappings

        # Build prompt with top mappings
        top_mappings = [m for m in mappings if m.confidence >= 0.7][:20]
        if not top_mappings:
            return mappings

        mapping_lines = []
        for m in top_mappings:
            mapping_lines.append(
                f"  [{m.source_service}] {m.source_entity} ↔ [{m.target_service}] {m.target_entity} (score={m.confidence})"
            )

        prompt = f"""You are analyzing a microservice system. Below are automatically-detected cross-service entity mappings (same business concept with different names in different services).

For each mapping, determine:
1. Whether they truly represent the SAME business concept
2. What the relationship is: "same" (identical concept), "subset" (one is part of the other), "wrapper" (one wraps/adapts the other), "related" (different but related concepts), or "false" (false positive)

Mappings:
{chr(10).join(mapping_lines)}

Output JSON:
{{
  "mappings": [
    {{
      "source_entity": "...",
      "target_entity": "...",
      "relationship": "same|subset|wrapper|related|false",
      "description": "Brief explanation of the relationship"
    }}
  ]
}}

JSON:"""

        response = _call_llm(prompt, max_tokens=500, temperature=0.1)
        data = _parse_json(response)
        if not data or "raw" in data:
            return mappings

        verified = data.get("mappings", [])
        if not verified or isinstance(verified, str):
            return mappings

        # Apply LLM corrections
        for m in mappings:
            for v in verified:
                if (v.get("source_entity") == m.source_entity and
                        v.get("target_entity") == m.target_entity):
                    m.relationship = v.get("relationship", m.relationship)
                    m.description = v.get("description", "")
                    if v.get("relationship") == "false":
                        m.confidence = 0.0
                    break

        return mappings

    # ── Formatters ──────────────────────────────────────────────────────

    def format_flow(self, flow: FlowDiagram) -> str:
        """Render the end-to-end flow diagram as text."""
        lines = [
            f"{'='*70}",
            f"End-to-End Flow: {flow.entry_service}",
            f"{'='*70}",
            f"  Entry API: {flow.entry_api or '(all APIs)'}",
            f"  Services involved: {flow.services_involved}",
            f"  Cross-service calls: {flow.total_cross_service_calls}",
            f"  Channels: {flow.channels_used}",
            "",
        ]

        if not flow.steps:
            lines.append("  No cross-service interactions found.")
            return "\n".join(lines)

        # Group steps by depth
        for step in flow.steps:
            indent = "  " * step.depth
            icon = {
                "http": "🌐", "kafka": "📨", "rabbitmq": "🐰",
                "nats": "✉️", "celery": "🥬", "sqs": "📦",
                "pubsub": "📢", "grpc": "🔌", "db": "🗄️",
                "internal": "→",
            }.get(step.channel, "→")

            if step.from_service.startswith("{"):
                # MQ topic hop
                lines.append(
                    f"{indent}{icon} [{step.channel.upper()}] {step.from_service}"
                    f"\n{indent}    └─→ {step.to_service}::{_short_name(step.to_function)}"
                    f"  ({step.detail})"
                )
            else:
                lines.append(
                    f"{indent}{icon} [{step.channel.upper()}] "
                    f"{step.from_service}::{_short_name(step.from_function)}"
                    f"\n{indent}    └─→ {step.to_service}::{_short_name(step.to_function)}"
                    f"  ({step.detail})"
                )

        return "\n".join(lines)

    def format_entities(self, entities: CrossServiceEntities) -> str:
        """Render cross-service entity mappings."""
        lines = [
            f"{'='*70}",
            f"Cross-Service Entity Alignment",
            f"{'='*70}",
            f"  Services: {entities.services}",
            f"  Entity mappings found: {len(entities.mappings)}",
            "",
        ]

        if entities.mappings:
            lines.append("  Mapped Entities:")
            for m in sorted(entities.mappings, key=lambda x: -x.confidence):
                conf_bar = "█" * int(m.confidence * 10) + "░" * (10 - int(m.confidence * 10))
                lines.append(
                    f"    [{m.source_service}] {m.source_entity:30s}"
                    f"  ↔  [{m.target_service}] {m.target_entity:30s}"
                    f"  {conf_bar} {m.confidence:.0%} [{m.relationship}]"
                )
                if m.description:
                    lines.append(f"      {m.description}")

        if entities.unmapped_entities:
            lines.append(f"\n  Unmapped Entities:")
            for svc, names in sorted(entities.unmapped_entities.items()):
                if names:
                    lines.append(f"    [{svc}] ({len(names)} entities)")
                    for n in names[:10]:
                        lines.append(f"      - {n}")
                    if len(names) > 10:
                        lines.append(f"      ... and {len(names) - 10} more")

        return "\n".join(lines)


def _short_name(qualified: str) -> str:
    """Shorten a qualified_name to just the last segment."""
    if not qualified:
        return ""
    return qualified.split("::")[-1] if "::" in qualified else qualified
