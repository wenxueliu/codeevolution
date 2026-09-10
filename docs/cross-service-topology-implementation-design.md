# 跨服务调用关系 Snapshot 化实现设计

> 状态：设计已确认，核心实现已落地（2026-09-10）；Tier-1 真实项目准确性、部署性能与生产授权策略仍需在目标环境验收。
>
> 范围：单个 Scope 内的服务拓扑、资源依赖、服务依赖影响、静态可能流程
>
> 上位设计：`docs/design.md`、`docs/repository-analysis-snapshot-technical-design.md`

实现入口：`codeevolution/analysis/communication/` 负责 Snapshot 通信事实，
`codeevolution/analysis/topology/snapshot_builder.py` 负责 Graph View Artifact，
`codeevolution/application/graph_artifact_service.py` 负责 Job/Cache，
`codeevolution/application/topology_query_service.py` 负责同步 Impact/Flow。
正式交付入口已经切换到 API、CLI、MCP 和 Web；旧 `/api/topology`、`/api/impact`、
`/api/flow` 及 CLI `trace`/`--no-cache` 已删除。

## 1. 背景与结论

当前代码存在两条没有接通的链路：

- `CrossRepoImplementation` / `AdvancedTopologyImplementation` 已包含 HTTP、MQ、gRPC、Redis、
  DB 的静态分析逻辑，但依赖 registry path、现场 CodeGraph DB 和现场源码；
- 当前 CLI、HTTP API、MCP 和 Web 使用 `SnapshotTopologyService`，但它从普通 API
  `call_chain` 中猜测 URL，不能稳定产生跨服务调用边，Impact 和 Flow 也只是简化投影。

本方案将完整的静态分析能力迁移到不可变 Repository Snapshot / Graph View 主链路，并确立两个
分析阶段：

```text
Repository Analysis Run
  -> frozen CodeGraph + manifest-limited frozen source
  -> CommunicationFactExtractor
  -> Repository Snapshot communication artifact

Single-Scope Graph View
  -> ResolvedGraphView(member -> immutable SnapshotHandle)
  -> TopologyArtifactBuilder
  -> immutable Topology Artifact
       -> synchronous Dependency Impact query
       -> synchronous Static Possible Flow query
  -> CLI / HTTP API / MCP / Web
```

核心边界：

1. Repository Snapshot 只固化目标无关的通信观测，不猜测目标仓库；
2. Graph View 阶段只在当前 Scope 固定的 Snapshot 集合内做跨仓匹配；
3. 一个 `RepositoryMember` 就是一个服务拓扑节点，不做多仓逻辑服务合并；
4. 构建过程不读取 registered path、现场源码或现场 `.codegraph`；
5. 服务调用、边界调用与资源依赖分开建模；
6. 只有目标身份可验证的调用才进入 confirmed service graph；
7. 每条 confirmed edge 必须可追溯到入口、调用点、调用链、规则、版本和置信度；
8. Topology 是唯一需要异步生成和持久缓存的跨仓 Artifact；Impact 和 Flow 从它同步派生。

## 2. 范围与非目标

### 2.1 本期范围

- 单个 Scope 内的 HTTP/HTTPS 同步服务调用；
- Kafka、RabbitMQ、Redis Pub/Sub、Redis Streams 异步调用；NATS 首期为 Experimental；
- gRPC 调用；
- Redis、SQL 数据库和其他基础设施资源依赖；
- Scope 外 HTTP/RPC/MQ 边界依赖；
- 服务级、端点级和 callsite 级三层拓扑；
- 上下游服务依赖分析；
- 按入口过滤、支持分支/汇合/循环的静态可能流程；
- Repository Snapshot 通信事实、Graph View Topology Artifact、Job 和 Cache；
- CLI、HTTP API、MCP、Web 的统一契约。

### 2.2 非目标

- 不跨 Scope 建立服务边；
- 不把多个 RepositoryMember 合并为一个服务节点；
- 不承诺解析所有动态 URL、反射、运行时服务发现或任意字符串拼接；
- 不把共享数据库或共享缓存推断为服务互调；
- 不在旧 Snapshot 缺少通信事实时回退读取现场仓库；
- 不实现跨服务实体对齐；
- 不实现跨 Snapshot 的代码变更影响分析；
- 本方案不讨论或设计 OTel、运行时 Trace 等动态遥测融合；
- 不保留旧 topology/impact/flow API、CLI 参数或响应结构兼容。

## 3. 身份与不可变性

### 3.1 Scope 和 Graph View

服务拓扑只接受单 Scope Graph View：

- `graph_views` 冻结一个 `scope_id`；
- View 创建请求只能选择一个 Scope；
- 显式选择 members 时，所有 member 必须属于同一 Scope；
- 混合 Scope 返回 `422 mixed_scope_members`；
- 历史数据中已存在的混合 Scope View 不做隐式拆分，生成 Topology 时返回
  `409 legacy_mixed_scope_view`，要求重新创建单 Scope View；
- topology/impact/flow 请求不再接受额外 scope 参数。

View digest 至少包含：

```text
scope_id
ordered member_id -> snapshot_id/availability mapping
frozen member display name
frozen declared service aliases
frozen scope topology rule digest
```

Member 后续重命名或别名修改只影响新 View，不能重新解释历史 View。

### 3.2 Service 身份

- `member_id` 是稳定的服务 ID，也是所有机器接口的主键；
- `display_name` 只用于展示和 CLI 精确解析；
- Web 展示 View 中冻结的 display name，但提交 member ID；
- CLI `--service <name>` 只能在 View 内做大小写敏感的精确唯一解析；
- 名称不存在或不唯一时返回错误并要求使用 member ID；
- Snapshot 内 node ID 只能与 snapshot ID 组合使用。


入口引用统一为：

```text
EntryRef(member_id, snapshot_id, entry_id)
```

Flow API 使用 `member_id + entry_id`；使用 method/path 的便捷选择最终也必须唯一解析为 EntryRef。

## 4. Repository Snapshot 通信事实

### 4.1 存储边界

详细通信观测不直接塞入通用知识 facts JSON。Repository Snapshot 使用：

```text
facts
  communication_summary
    schema_version
    artifact_key
    payload_digest
    byte_size
    completeness summary
    observation counts

snapshot communication artifact
  entries
  http_outbounds
  message_publications
  message_subscriptions
  grpc_clients
  grpc_servers
  resource_accesses
  unresolved_observations
  collector coverage
```

通信 Artifact 与 Repository Snapshot 一起原子发布，内容摘要进入 Snapshot facts/manifest 身份。小仓库和
大仓库使用相同领域 schema；底层可选择 inline 或内容寻址文件存储，但查询契约不变。

建议 schema 名称：

```text
repository-communication/v1
topology-normalization/v1
```

### 4.2 通用证据类型

`Location`：

```json
{
  "file": "src/orders/api.py",
  "start_line": 42,
  "start_column": 5,
  "end_line": 42,
  "end_column": 61
}
```

- file 必须是 Evidence manifest 中的 NFC/POSIX 相对路径；
- 禁止绝对路径、`..` 和 symlink 逃逸；
- 行号从 1 开始；无法获取的列使用 null，不虚构为 0。

`NodeRef`：

```json
{
  "snapshot_id": "snapshot-uuid",
  "node_id": "method:local-id",
  "kind": "method",
  "name": "create_order",
  "qualified_name": "orders.api.create_order",
  "location": {}
}
```

`CallPathEvidence`：

```json
{
  "entry_ref": {},
  "callsite": {},
  "nodes": [],
  "edge_kinds": ["calls", "calls"],
  "depth": 2,
  "truncated": false,
  "max_depth": 12,
  "reachability_rule": "shortest-call-path/v1",
  "alternative_shortest_path_count": 0
}
```

路径必须从入口 handler 开始，以 client/resource 调用点结束。相同长度的多条最短路径按 node semantic
key 稳定排序后选第一条，并记录候选数量。

### 4.3 入口事实

统一识别以下生产入口：

- HTTP route；
- 消息消费者；
- gRPC server method；
- cron/scheduled job；
- Celery、RQ 等 task-worker handler；
- CLI/人工运维命令；
- 框架声明的 startup/background entry。

入口 schema：

```json
{
  "entry_id": "entry:sha256:...",
  "kind": "http",
  "protocol": "http",
  "method": "POST",
  "path_template": "/api/orders/{id}",
  "channel": "",
  "handler": {},
  "evidence": {}
}
```

`entry_id` 由 kind、协议身份和 handler semantic key 的 canonical JSON 生成，在一个 Snapshot 内唯一。

默认排除测试、benchmark、example、migration 和开发脚本中的入口。无法归属生产入口的调用点只进入
诊断型 `unresolved_observations`，不得成为 confirmed service edge。

只有从上述入口沿冻结 CodeGraph `calls` 边可达的出站调用才能成为依赖观测。默认单仓最大深度为 12；
深度或数量截断使对应 collector 变为 `truncated`，不能继续声称 complete。一个调用点被多个入口到达时，
按 `(entry_id, callsite)` 分别保存。

### 4.4 HTTP 出站观测

```json
{
  "observation_id": "http:sha256:...",
  "entry_ref": {},
  "caller": {},
  "callee_node_id": "external:requests.post",
  "client": {
    "library": "requests",
    "operation": "post",
    "symbol": "requests.post"
  },
  "request": {
    "method": "POST",
    "method_resolution": "client_operation",
    "raw_url_template": "http://user-svc/api/users/{id}",
    "scheme": "http",
    "authority": "user-svc",
    "normalized_path": "/api/users/{param}",
    "query_keys": [],
    "resolution": "complete"
  },
  "client_binding": {
    "client_instance": "user_client",
    "base_authority": "user-svc",
    "config_key": "USER_SERVICE_URL",
    "source": "frozen_config",
    "resolution": "static"
  },
  "callsite": {},
  "call_path": {},
  "extraction_confidence": 0.96,
  "evidence": {}
}
```

约束：

- URL/method 从冻结 AST/图/源码和非敏感冻结配置提取，禁止执行用户代码；
- 常量传播限制在局部常量、模块常量和明确配置默认值；
- base URL + relative path 必须保存推导来源；
- 只有 client binding 能唯一映射服务时，相对 URL 才可能形成 confirmed service edge；
- 无 binding 的相对 URL 即使 path 在 View 中唯一，也只能成为 unresolved candidate；
- raw URL 落盘前删除 userinfo、query value、token 和 fragment，只保留 query key；
- 无法解析 URL 仍保留 observation，不能静默丢弃。

### 4.5 消息观测

生产者和消费者分别保存，Repository Snapshot 阶段不做跨仓配对：

```json
{
  "observation_id": "message:sha256:...",
  "direction": "publish",
  "entry_ref": {},
  "function": {},
  "messaging": {
    "protocol": "kafka",
    "broker_instance_hint": "kafka:orders",
    "destination_kind": "topic",
    "exchange": null,
    "channel": "orders.created",
    "routing_key": null,
    "queue": null,
    "consumer_group": null,
    "event_type": "OrderCreated",
    "delivery_semantics": "broadcast",
    "name_resolution": "literal"
  },
  "callsite": {},
  "call_path": {},
  "evidence": {}
}
```

规则：

- producer 必须从正式入口可达；consumer 自身就是入口；
- protocol 和 channel/queue 都必须解析才能配对；
- 空 channel 只进入 unresolved，禁止通配所有消费者；
- Kafka 按 topic 和 consumer group 判断 fan-out/competing；
- RabbitMQ 按 exchange type、routing key、binding 和 queue 判断；
- NATS 按 subject 与 queue group 判断；
- Redis Pub/Sub 通常是 broadcast；Redis Streams 按 stream 与 consumer group 判断；
- broadcast 产生多个 confirmed branches；
- competing consumers 形成 alternatives group，不声称每个消费者都会执行；
- delivery semantics 无法确定时保留 candidate，不进入 confirmed Flow。

Redis `PUBLISH/SUBSCRIBE` 和 `XADD/XREADGROUP` 只生成消息观测，不重复生成为数据资源观测；普通
cache/data 命令只生成资源观测，避免把共用 broker 误报为 `potential_data_coupling`。

### 4.6 gRPC 观测

```json
{
  "observation_id": "grpc:sha256:...",
  "direction": "client",
  "entry_ref": {},
  "function": {},
  "rpc": {
    "package": "users.v1",
    "service": "UserService",
    "method": "GetUser",
    "fully_qualified_method": "/users.v1.UserService/GetUser",
    "authority": "user-svc:9090",
    "streaming": "unary",
    "identity_resolution": "generated_stub"
  },
  "callsite": {},
  "call_path": {},
  "evidence": {}
}
```

优先从 proto descriptor、生成的 stub 和 server registration 提取 package/service/method。仅靠 callee name
substring 的结果只能成为 candidate，不能生成高置信 confirmed edge。

### 4.7 资源观测

```json
{
  "observation_id": "resource:sha256:...",
  "entry_ref": {},
  "function": {},
  "resource": {
    "type": "redis",
    "instance_id": "redis://cache:6379/2",
    "namespace": null,
    "object": "order:{param}",
    "operation": "SET",
    "access_mode": "write",
    "instance_resolution": "sanitized_config"
  },
  "callsite": {},
  "call_path": {},
  "evidence": {}
}
```

- instance ID 必须脱敏；优先使用安全 URL、host/port/database 和非敏感配置引用；
- 无法解析实例时使用 `unresolved:<member_id>:<resource_type>`，不得使用跨服务共享的全局 default ID；
- SQL 保存 database/schema/table/operation；未知值显式使用 unknown；
- 共享 DB/Redis 只形成资源关联；默认 Impact 不沿共享资源扩散；
- 显式 `include_shared_resource_risks=true` 时返回 `potential_data_coupling`，仍不进入 confirmed service
  graph 或 StaticPossibleFlow。

### 4.8 完整性

每个 collector 返回：

```text
complete | truncated | unsupported | failed | unavailable
```

并附带 `reason`、`coverage`、`truncated_count`、collector/rule version。顶层通信完整性为：

- 全部适用且启用的 Tier-1 collector complete -> complete；
- 任一 collector truncated/unsupported/failed -> partial；
- 必要图或源码整体不可用 -> unavailable。

这里的 complete 只表示“在声明的 Tier-1 支持矩阵和分析预算内完成”，不表示覆盖任意动态代码。

## 5. Collector 架构

新增：

```text
codeevolution/analysis/communication/
  extractor.py
  entry_collector.py
  reachability.py
  http_collector.py
  message_collector.py
  grpc_collector.py
  resource_collector.py
  url_resolver.py
  normalization.py
```

统一接口：

```python
class CommunicationCollector(Protocol):
    def collect(
        self,
        graph: CodeGraphRepository,
        sources: SnapshotSourceProvider,
        rules: CollectorRuleSet,
    ) -> CollectorResult: ...
```

接口不接收 repository `Path`。生产 adapter 只能打开 Repository Attempt 已冻结的 graph 和 manifest 限定
source inventory。

共同流程：

1. 识别所有正式入口；
2. 一次性构建 calls adjacency 和 node index；
3. 按协议规则收集 client/resource callsite；
4. 执行多源或逐入口 BFS；
5. 只为入口可达调用生成 observation；
6. 选取确定性最短路径并保留证据；
7. 输出 resolved observation、unresolved 和 coverage；
8. 与知识报告一起参与 Repository Snapshot 发布。

图边缺失但源码模式命中时，只能产生低置信 unresolved observation，不能绕过入口可达硬条件。

## 6. Tier-1 支持矩阵

首期 Tier-1：

| 语言 | Server Entry | HTTP Client | MQ | gRPC | Redis/DB |
|---|---|---|---|---|---|
| Python | FastAPI/Flask | requests/httpx/aiohttp | kafka-python、confluent-kafka、pika | grpcio | redis-py、SQLAlchemy、DB-API |
| TypeScript | Express/NestJS | fetch/axios | Experimental | Experimental | Experimental |
| Java | Spring MVC/WebFlux | RestTemplate/WebClient/OpenFeign | Spring Kafka、Spring AMQP | grpc-java | Jedis/Lettuce、JDBC/JPA |
| Go | net/http/Gin | net/http client | Experimental | grpc-go | Experimental |

现有其他语言和规则全部标记为 Experimental。支持矩阵放入版本化规则文件并参与 `rules_digest`，文档由
规则校验或生成，避免漂移。

框架晋升 Tier-1 必须满足：

- 具有 Snapshot -> Topology golden E2E；
- golden fixture confirmed edge 召回率 100%、假阳性为 0；
- 至少参与 2–3 个真实多仓项目人工标注基准；
- 真实项目 confirmed edge precision >= 95%；
- 真实项目 recall >= 85%；
- ambiguous/out-of-scope/unresolved 分类单独报告。

## 7. Graph View 解析和服务别名

### 7.1 ResolvedGraphView

```python
@dataclass(frozen=True)
class SnapshotHandle:
    member_id: str
    snapshot_id: str
    display_name: str
    declared_aliases: tuple[str, ...]
    facts_digest: str
    communication_artifact_key: str
    completeness: str

@dataclass(frozen=True)
class ResolvedGraphView:
    scope_id: str
    view_id: str
    view_digest: str
    members: tuple[SnapshotHandle | UnavailableMember, ...]
```

Resolver 验证 Snapshot 属于 member、未删除、digest 正确、schema 受支持。View 原生 unparsed/retired 或
collector 降级允许生成 200 partial；原本 available 的 Snapshot 丢失、digest 错误或 Artifact 损坏属于
输入完整性失败，Job failed，查询返回 424，不发布 Topology。

### 7.2 别名来源

允许 Scope 管理员维护声明式：

- 服务 host/authority aliases；
- base path；
- 消息通道映射；
- 资源实例别名。

Snapshot 也可从冻结 Docker Compose、Kubernetes、框架配置和非敏感服务发现配置中提取别名。每个别名
保存 value、source 和 confidence。禁止上传或执行自定义 matcher 代码。

创建 View 时冻结 member display name、用户声明别名和规则摘要。用户声明优先于自动 hint，但不能覆盖
身份冲突；多个 member 声明同一 alias 时生成 `ambiguous_alias`，禁止按遍历顺序选择。
由名称大小写、连字符、下划线等机械变换产生的别名只能生成 candidate，不能单独确认服务身份。

## 8. 跨仓匹配

### 8.1 硬条件与置信度

候选生成和置信度计算分开。Confirmed service edge 必须先满足协议硬条件：

- 来源 observation 有正式入口可达证据；
- 目标身份可通过冻结 host/base binding、用户声明 alias、冻结配置 alias、RPC identity 或消息地址验证；
- 目标唯一属于当前 View/Scope；
- protocol-specific method/path/channel/RPC identity 相容；
- 无身份冲突。

然后计算可解释置信度：

```json
{
  "score": 0.95,
  "level": "high",
  "components": [
    {"name": "extraction", "score": 0.95, "rule": "ast-literal"},
    {"name": "reachability", "score": 1.0, "rule": "shortest-call-path/v1"},
    {"name": "protocol_identity", "score": 1.0, "rule": "method+route"},
    {"name": "target_identity", "score": 0.9, "rule": "frozen-service-alias"}
  ],
  "caps": [],
  "warnings": []
}
```

等级固定为 high >= 0.90、medium 0.80–0.899、low < 0.80。只有满足硬条件且达到 medium/high 的结果
进入 confirmed graph；low 结果进入 candidate。权重、阈值和 caps 属于版本化规则内容。

### 8.2 HTTP

索引：

```text
frozen host alias -> member_id
(method, normalized route segment count) -> inbound endpoint candidates
```

匹配顺序：

1. authority/client binding 唯一映射到 View 内 member；
2. 只在该 member 的 inbound endpoints 中匹配；
3. method 必须一致，未知 method 只能进入 candidate；
4. segment matcher 支持 `:id`、`{id}`、`<id>`、框架 wildcard/regex、trailing slash 和显式 base path；
5. 唯一最高候选 score >= 0.80 且比第二名至少高 0.10，生成 confirmed endpoint dependency；
6. service 已确定但 endpoint 并列时，service projection 可 confirmed，endpoint resolution 为 alternatives；
7. 无 binding 的相对 URL 只记 unresolved；
8. 明确 host 未命中 View 时记 `out_of_scope_or_unregistered`，不自动声称 third-party external；
9. View 内 alias 冲突或目标候选同分时记 ambiguous。

### 8.3 MQ

匹配键包含 protocol、broker/cluster hint、destination kind、topic/exchange/queue/routing key 和 consumer
group。使用各 broker 的真实 wildcard/binding 语义，禁止 substring 泛匹配。

- broadcast -> 每个 consumer service 一条 confirmed endpoint dependency；
- competing -> 一个 alternatives group；
- unknown delivery semantics -> candidate；
- 无 consumer 的 publisher -> unresolved/out-of-scope boundary；
- 空 channel -> unresolved。

### 8.4 gRPC

优先顺序：

1. fully-qualified method；
2. package + service + method；
3. service + method 且 View 内唯一；
4. frozen authority alias 消歧；
5. 仅名称启发式保留 candidate。

### 8.5 Scope 外边界

Scope 内 service graph 只包含 confirmed internal edges。目标不在 View 的调用放入独立
`BoundaryDependency`：

```text
known_external        命中明确的第三方/外部配置规则
out_of_scope_or_unregistered
                      有 host/identity，但无法静态区分 Scope 外服务和尚未注册的服务
unresolved            目标身份无法解析
ambiguous             多个内部候选无法消歧
```

边界依赖可以展示，但不创建 Scope 内 ServiceNode，不参与 Impact/Flow 的继续遍历。

## 9. 三层拓扑模型

### 9.1 Observation

一个 entry + callsite 对应一条目标无关单仓事实，属于 Repository Snapshot。

### 9.2 EndpointDependency

按以下字段聚合：

```text
protocol
source EntryRef
target EntryRef / endpoint alternatives
transport identity
```

同一关系可包含多个 supporting observation IDs。聚合置信度采用最佳可解释证据，不做平均。

### 9.3 ServiceProjection

按 `(source_member_id, target_member_id, protocol)` 聚合，仅用于服务图展示和 Impact 索引，保留
supporting endpoint dependency IDs。Flow 必须使用 EndpointDependency，不能从服务级投影反推入口。

共享资源单独形成 ResourceDependency，不投影为 ServiceProjection。

## 10. Topology Artifact

### 10.1 内容身份

每个 `(view_digest, analyzer_bundle_digest, rules_digest, topology_schema_version)` 只生成一份完整 Topology，
不按 channel、service 或 min confidence 生成多份。过滤属于同步查询或 UI 行为。

Artifact payload 不包含 view ID，以支持相同 view digest 的不同 View 复用。交付 envelope 才携带本次
请求的 view ID。

```json
{
  "schema_version": "topology-artifact/v1",
  "artifact_kind": "topology",
  "view_digest": "sha256:...",
  "scope_id": "scope-uuid",
  "members": [],
  "services": [],
  "endpoint_dependencies": [],
  "service_projections": [],
  "message_alternatives": [],
  "resource_dependencies": [],
  "boundary_dependencies": [],
  "candidates": [],
  "coverage": {},
  "warnings": [],
  "identity": {},
  "statistics": {}
}
```

统一 edge 字段：

```text
edge_id (canonical body SHA-256)
kind (http | message | grpc | resource | boundary)
source member/snapshot/entry/function
target typed reference
transport typed payload
confidence score/level/components
match rule/version/rules_digest
supporting observation IDs
typed evidence
```

所有数组按稳定 semantic key 排序；时间、View label、registered path 不参与 payload digest。同一输入重复
构建必须产生 byte-identical canonical payload。

### 10.2 完整性和未知边界

允许缓存 partial Topology，但必须包含：

- complete members；
- missing/unavailable members；
- member + protocol collector status；
- truncated counts；
- unknown boundaries；
- possible-but-unresolved candidates。

如果 View 没有任何可分析 Snapshot，不创建 Job，返回 `409 view_has_no_analyzable_members`。至少一个
Snapshot 可用时允许 partial。已绑定可用 Snapshot 的 Artifact 丢失或损坏是 424 hard failure，不发布
partial。

## 11. 服务依赖影响

Impact 是 Topology 上的同步查询，不保存独立 Artifact。它表达 dependency impact，而不是代码变更影响。

默认参数：

```text
direction = both
max_depth = 5 (hard max 20)
channels = http,message,grpc
include_resources = true
include_shared_resource_risks = false
include_candidates = false
```

输出明确区分：

- direct/transitive upstream dependents；
- direct/transitive downstream dependencies；
- shortest service paths；
- subject service resources；
- optional potential data coupling；
- unresolved risks 和 unknown boundary。

使用 service projection 的正向/反向 BFS。Partial 结果不能输出确定性的“无上游/无下游”，而应使用
`no_confirmed_*_within_coverage` 并附 coverage。

## 12. 静态可能流程

Flow 是 Topology 上的同步查询，不保存独立 Artifact。它表示 `StaticPossibleFlow`，不声称运行顺序、
调用次数、条件必然性或并发关系。

默认参数：

```text
member_id = required
entry_id = required, or uniquely resolved method+path
max_depth = 8
max_nodes = 500
max_edges = 1000
channels = http,message,grpc
include_resources = false
include_candidates = false
```

返回 rooted directed graph：

```json
{
  "root": {},
  "nodes": [],
  "edges": [],
  "references": [],
  "cycles": [],
  "coverage": {},
  "truncation": null
}
```

规则：

- 状态节点 key 为 `(member_id, EntryRef)`；
- HTTP/gRPC 到达目标 handler EntryRef 后，只从该入口继续；
- MQ publish -> channel -> consumer 视觉上可分三段，逻辑深度只增加 1；
- broadcast 形成确定分支；competing 形成互斥 alternatives；
- resource 是可选终止节点，不继续传播；
- Scope 外 boundary 是终止节点；
- 再次到达已展开状态时生成 `cycle_ref` 或 `cross_ref`，不继续展开；
- 到达 max depth/nodes/edges 时保存未展开 frontier 和稳定截断原因；
- partial/unsupported/unresolved 边界生成 unknown frontier；
- `include_candidates=true` 只添加虚线候选边，不把它们计入 confirmed paths。

## 13. Topology Job 与 Cache

### 13.1 显式创建、只读查询

GET 绝不创建 Job：

```text
GET topology -> 200 ready | 404 artifact_not_generated
POST artifact-jobs -> 200 cache hit | 202 existing/new active job
```

Web 打开 Graph View 时只执行 GET，未生成时显示“生成拓扑”按钮；用户点击后才 POST。

### 13.2 Request Spec 和 Cache Key

Job 持久化不可变 `request_spec_json`：

```json
{
  "identity_schema": "graph-artifact-key/v1",
  "view_digest": "sha256:...",
  "scope_id": "scope-uuid",
  "members": [{"member_id": "...", "snapshot_id": "..."}],
  "artifact_kind": "topology",
  "normalized_params": {},
  "analyzer_bundle_digest": "sha256:...",
  "rules_digest": "sha256:...",
  "artifact_schema_version": "topology-artifact/v1"
}
```

Request spec digest 是 Job 的审计身份。Cache key 为以上 canonical JSON 的 SHA-256。Retry 精确重放原
spec；使用新分析器/规则/schema 重算是新 key 和新 Job，不是 retry。原 builder 版本已不可执行时
返回 `409 unsupported_artifact_builder_version`，不得以当前 builder 冒充 retry。

### 13.3 状态机、lease 和取消

```text
pending -> running -> completed
                  |-> failed
                  |-> cancelled
                  |-> interrupted
```

Job 包含 `worker_id`、`lease_token`、`lease_until`、`heartbeat_at`、stage、progress、attempt_no 和
retry_of_job_id。Worker 用数据库事务原子 claim；执行语义是 at-least-once，cache key CAS/唯一约束保证
单一 canonical 结果。过期 lease 转 interrupted，新 attempt 可精确重放；旧 worker 通过 fencing token
禁止提交。

相同 cache key 只允许一个 active Job。取消是共享计算的全局管理操作，不是取消订阅：

- Web/MCP 普通调用者只能停止轮询；
- cancel 仅供管理员，或创建了该 Job 且确认 Job 未被其他调用者共享的本地 CLI owner；
- 未建立权限模型前不公开 cancel HTTP endpoint；
- pending 可直接取消；running 设置 cancellation request，由 worker 在安全检查点停止；
- worker 真正终止后才释放 Snapshot 引用；
- completed 不删除 Cache。

### 13.4 发布和引用

1. 为输入 Snapshot 创建 temporary `artifact_job` reference；
2. 在 staging 写 payload 和 manifest；
3. 计算摘要并重新读取校验；
4. 原子发布内容寻址 Artifact；
5. 数据库事务登记 cache 并完成 Job；
6. 所有终态释放 temporary job reference，不转永久。

Topology payload 自包含拓扑查询和基础证据。完整源码/调用树仍由 Snapshot API 提供；Snapshot 已删除时
返回 `snapshot_gone`。Pinned/有效 View 保护 Snapshot，Cache 不永久保护输入。

小于 1 MiB 的 canonical payload 可 inline；大于等于 1 MiB 存内容寻址 Artifact，Job/Cache 不重复保存
完整 JSON。

### 13.5 版本选择和保留

- 默认 GET 查询当前 analyzer/rules/schema identity；
- 当前版本未生成返回 404，不自动使用旧版本；
- UI 可列出旧 Artifact，并提示显式生成当前版本；
- 旧 Artifact 不标 stale，只标 `not_current`；
- Artifact 只能经有效 View 授权读取，不能凭 cache key 绕过 View；
- 相同 digest 的有效 View 可以复用 Cache；
- Ephemeral View 过期后不能再访问；Pinned View 可在 Cache 清理后重建。

默认 retention：

```text
Topology Cache: last access + 30 days
Completed/failed Job metadata: 90 days
Job log excerpt: 30 days
```

TTL/LRU 可配置；服务端长期可重建性通过 pin View 保证。CLI 的 `--output` 是调用者持有的离线导出，
不进入服务端 Artifact 生命周期。

### 13.6 存储迁移

数据库以向前迁移增加以下持久字段，具体 SQL 由现有 storage adapter 的 migration 机制管理：

- `graph_views.scope_id`、冻结规则摘要和 View identity schema；
- `graph_view_members` 的冻结 display name、声明别名及 Snapshot availability；
- Repository Snapshot communication summary 的 Artifact key、digest、size、counts 和 completeness；
- Graph Artifact Job 的完整 request spec/digest、stage/progress、attempt/retry、worker/lease/fencing、
  heartbeat 和 cancellation request；
- Graph Artifact Cache 的完整 identity、payload/manifest digest、存储位置、大小和 last-access 时间。

迁移后为 cache identity 和 active Job identity 建唯一约束。旧 View 无 `scope_id` 时通过成员反查：唯一
Scope 可只读补齐；多个 Scope 则标记为 `legacy_mixed_scope_view`，不得猜测选择。

## 14. HTTP API

正式 API：

```http
GET  /api/graph-views/{view_id}/artifacts/topology
GET  /api/graph-views/{view_id}/artifacts/topology/history
POST /api/graph-views/{view_id}/artifact-jobs

GET  /api/graph-artifact-jobs/{job_id}
POST /api/graph-artifact-jobs/{job_id}/retry

GET  /api/graph-views/{view_id}/impact
     ?member_id=&direction=&max_depth=&channels=&include_resources=
     &include_shared_resource_risks=&include_candidates=
GET  /api/graph-views/{view_id}/flow
     ?member_id=&entry_id=&method=&path=&max_depth=&max_nodes=&max_edges=
     &channels=&include_resources=&include_candidates=
```

取消 endpoint 仅在权限模型可验证管理员身份后公开。

创建请求只接受无过滤参数的完整 Topology：

```json
{
  "artifact_kind": "topology",
  "params": {}
}
```

其他 kind、未知字段或非空 params 返回 `422 invalid_artifact_request`。Cache hit 的 200 响应返回
Topology 交付 envelope；复用或新建 active Job 的 202 响应返回 `job_id`、`status`、`reused`、
`status_url` 和 `retry_after_seconds`。

Topology 交付 envelope：

```json
{
  "view_id": "request-view-id",
  "artifact_url": "/api/graph-views/.../artifacts/topology",
  "payload_digest": "sha256:...",
  "artifact": {
    "view_digest": "sha256:..."
  }
}
```

Cache hit 返回 200 + ETag；POST 新建/复用 Job 返回 202 + Location + Retry-After。所有接口进入 OpenAPI。

错误契约：

```json
{
  "error": {
    "code": "view_not_found",
    "message": "...",
    "details": {},
    "request_id": "req-..."
  }
}
```

主要错误：

| HTTP | code |
|---|---|
| 400 | `malformed_request` |
| 404 | `view_not_found` / `artifact_not_generated` / `job_not_found` |
| 409 | `view_has_no_analyzable_members` / `legacy_mixed_scope_view` / `artifact_generation_failed` / `unsupported_artifact_builder_version` |
| 410 | `view_expired` / `snapshot_gone` |
| 422 | `invalid_artifact_request` / `mixed_scope_members` / `member_not_in_view` / `entry_not_in_view` / `ambiguous_entry` |
| 424 | `snapshot_unavailable` / `snapshot_artifact_corrupt` |
| 503 | `artifact_scheduler_unavailable` |
| 507 | `insufficient_storage` |

不保留 `/api/topology`、`/api/impact`、`/api/flow`。实体对齐接口不属于本文范围。

## 15. CLI、MCP 和 Web

### 15.1 CLI

```bash
codeevolution topology --view-id VIEW [--server URL] [--wait|--no-wait] \
  [--timeout SECONDS] [--poll-interval SECONDS] [--output topology.json]
codeevolution impact --view-id VIEW --service MEMBER_OR_EXACT_NAME \
  [--direction both] [--max-depth 5] [--channels http,message,grpc] \
  [--no-resources] [--include-shared-resource-risks] [--include-candidates] [--output impact.json]
codeevolution flow --view-id VIEW --service MEMBER_OR_EXACT_NAME \
  (--entry-id ENTRY | --method METHOD --path PATH) [--max-depth 8] \
  [--max-nodes 500] [--max-edges 1000] [--channels http,message,grpc] \
  [--include-resources] [--include-candidates] [--output flow.json]
```

- 本地 CLI 默认创建/驱动 Job 并等待完成；
- 连接常驻 `--server` 时默认等待，显式 `--no-wait` 才只输出 Job ID；
- 本地模式使用 `--no-wait` 是参数错误；远程等待时 Ctrl-C 只停止轮询，不取消共享 Job；
- Impact/Flow 要求当前 Topology 已生成；CLI 可提示先运行 topology，但不隐式切回旧算法；
- `--output` 将已完成的 Topology 或同步查询结果作为 canonical JSON 原子写入本地文件，不创建新的
  服务端 Artifact；
- Ctrl-C 仅在本地 CLI 新建并独占 Job 时协作取消；复用已有 Job 时只停止等待。退出码均为 130；
- 删除 `trace`、`--no-cache` 和 topology `--service`；实体对齐命令不属于本文范围；
- JSON 输出保持领域 DTO，面向人的进度写 stderr。

统一退出码：`0` 成功、`2` 参数或 selector 错误、`3` Artifact 未生成/仍在运行、`4` 分析失败、
`5` 输入 Snapshot/View 已失效、`130` 用户中断。

### 15.2 MCP

- `get_topology_artifact` 只读，未生成返回结构化 `artifact_not_generated`；
- `generate_topology_artifact` 显式创建/复用 Job；
- `get_graph_artifact_job` 查询进度；
- `query_dependency_impact` 和 `query_static_flow` 同步读取 Topology；
- MCP 不暴露共享 Job cancel；
- 所有 selector 使用稳定 member/entry ID。

### 15.3 Web

Graph View 页面：

1. 打开时只读查询 Topology；
2. 未生成时显示“生成拓扑”按钮；
3. 显式创建后按 Retry-After 轮询 Job；
4. 离开页面仅停止轮询，不取消共享 Job；
5. Topology 完成后启用 Impact 和 Flow 同步查询；
6. View 过期时提示创建新 View，不静默切换；
7. 新 Snapshot 发布不改变当前页面的固定 View。

展示分区：

- Scope 内 confirmed service graph；
- endpoint/callsite 证据；
- message alternatives；
- resources 和 potential data coupling；
- known external/out-of-scope/unresolved/ambiguous；
- coverage、partial members 和 unknown boundaries；
- upstream/downstream dependency impact；
- rooted StaticPossibleFlow、分支、汇合、循环和截断。

## 16. 代码结构

```text
codeevolution/
  domain/
    topology.py
    graph_artifact.py
  ports.py
  analysis/
    communication/
      extractor.py
      entry_collector.py
      reachability.py
      http_collector.py
      message_collector.py
      grpc_collector.py
      resource_collector.py
      url_resolver.py
      normalization.py
    topology/
      snapshot_builder.py
      http_matcher.py
      message_matcher.py
      grpc_matcher.py
      resource_projector.py
      impact.py
      flow.py
      rules.py
  application/
    graph_view_resolver.py
    graph_artifact_service.py
    graph_artifact_scheduler.py
    graph_artifact_worker.py
    topology_query_service.py
  infrastructure/
    codegraph_sqlite.py
    snapshot_source.py
    analysis_snapshot_sqlite.py
    artifact_store_fs.py
  delivery/
    topology_renderer.py
```

迁移完成后：

- `SnapshotTopologyService` 删除猜测式分析逻辑；
- `cross_repo_impl.py` / `advanced_impl.py` 的可复用算法迁入 collector/matcher；
- analysis-local topology dataclass 迁入 domain；
- registry topology JSON cache 删除；
- API、CLI、MCP 不再自行分析或拼装自由 dict；
- `cross_repo.py`、`p2_advanced.py` 删除或仅保留非公开内部迁移 facade；
- 所有旧公开入口和参数直接删除，不提供兼容层。

## 17. 分阶段实施

### Phase 0：契约与失败测试

1. 定义 topology/graph artifact DTO、JSON schema、错误码和 canonicalization；
2. 为当前 call chain 无 URL 导致 Snapshot topology 无边补回归测试；
3. 建立三服务 HTTP/MQ/gRPC/Redis/DB golden fixture；
4. 固定 observation/edge ID、payload digest 和 cache key 测试；
5. 增加单 Scope View 约束和冻结 display/alias schema。

### Phase 1：Repository communication artifact

1. 实现 entry/reachability collector；
2. 迁移 HTTP、MQ、gRPC、Redis/DB collector 到冻结 graph/source ports；
3. 在 Repository Attempt ANALYZING 阶段生成 communication artifact 和 summary；
4. 升级 report/analyzer/rules schema identity；
5. 建立 Tier-1 支持规则文件和 golden tests；
6. 验证移动/删除现场仓库后 Snapshot 结果不变。

### Phase 2：Topology Artifact

1. 实现 GraphViewResolver 和单 Scope 校验；
2. 实现 HTTP/MQ/gRPC matcher、resource projector 和 boundary classification；
3. 输出 observation -> endpoint -> service 三层 typed topology；
4. 实现 partial coverage、unknown boundaries 和 deterministic payload；
5. 接入显式 Job、lease scheduler、cache、引用和内容寻址发布。

### Phase 3：同步 Impact 和 Static Flow

1. 实现 service projection 正反 BFS；
2. 实现 endpoint-rooted Flow graph、分支、汇合、alternatives、cycle/cross refs；
3. 实现默认限制、truncation 和 partial unknown frontier；
4. 实现资源风险 opt-in 和 Scope boundary 终止语义。

### Phase 4：交付切换与清理

1. API、CLI、MCP、Web 一次性切换到新契约；
2. Web 增加显式生成、Job 轮询和 typed topology 展示；
3. 删除旧 API、CLI、cache、DTO 和不可达实现；
4. 更新 `AGENTS.md`、README、CLI help、OpenAPI 和设计状态；
5. 执行全量构建、重启和冒烟验证。

## 18. 测试与门禁

### 18.1 单元测试

- URL/base binding/alias 规范化和脱敏；
- path template、method、host、RPC identity 硬条件；
- confidence components、threshold、caps；
- 空 channel、broker wildcard、broadcast/competing；
- Redis message/resource 分离；
- resource instance canonicalization；
- 三层聚合与最佳证据选择；
- canonical sort、edge ID、payload/cache digest；
- Impact 正反 BFS；
- Flow branch/merge/cycle/cross-ref/truncation。

### 18.2 Snapshot 集成测试

- frozen graph/source -> communication artifact；
- 现场仓库移动/删除后结果不变；
- 混合 View 中旧成员缺 communication summary -> partial，不 live fallback；全部成员都缺失 -> 409；
- source/graph 不一致、缺文件和坏 digest；
- Tier-1 每个框架的 collector coverage。

### 18.3 Graph View E2E

```text
gateway --HTTP--> orders --HTTP--> users
   |                  |--Kafka--> users
   |                  |--gRPC---> users
   |                  |--Redis/DB resource
   +------------------------------> users
users --HTTP--> gateway
```

覆盖：

- 单 Scope enforcement；
- host/binding 消歧、相对 URL unresolved、同 endpoint alternatives；
- out-of-scope 和 known external；
- 不可从入口到达的调用排除；
- broadcast/competing/unknown message；
- Redis Pub/Sub/Streams 与 cache resource；
- partial/unparsed/retired/corrupt Snapshot；
- 新 Snapshot 不改变旧 View；
- 规则升级产生新 Cache；
- 删除原仓库后仍能重建相同 payload digest。

### 18.4 Job/Store/API/CLI/Web

- GET 无副作用，POST 并发去重；
- 20 个并发请求只有一个 active Job；
- claim/lease/heartbeat/fencing/restart interrupted/retry；
- cooperative cancel 和 reference release；
- inline/external payload 分界及摘要损坏；
- 200/202/404/409/410/422/424/503/507 契约；
- OpenAPI 包含全部正式接口；
- CLI local/remote wait、no-wait、Ctrl-C 和退出码；
- Web 显式生成、轮询、partial、boundary、Impact 和 Flow。

### 18.5 性能门禁

基准规模：100 个服务、10 万图节点、1 万 observations、5 万 endpoint dependencies。

- collector 峰值内存不超过图索引大小 2 倍；
- topology build P95 < 10 秒；
- cached topology P95 < 200 ms；
- 1 万服务边下 Impact/Flow P95 < 1 秒；
- 默认 Flow hard limit 生效且稳定返回 frontier；
- 相同 Artifact identity 的并发请求不重复发布。

## 19. 安全与保留

- SnapshotSourceProvider 只能读取 manifest 内相对路径；
- 不执行用户源码或配置；
- URL、连接串、消息地址在落盘前统一脱敏；
- Artifact 不包含 registered absolute path、完整源码、secret、query value 或 header value；
- 日志、错误、preview 和 payload 字段限长；
- member、observation、edge、BFS depth/nodes/edges 和 payload 都有硬上限；
- partial 与 empty 严格区分；
- Artifact 读取必须通过仍有效的 View 授权；
- Cache 清理前确认没有 active reader/lease。

## 20. 旧数据与破坏性迁移

- 旧 Snapshot 保持知识只读；没有 communication artifact 时标记
  `communication_facts_unavailable`；
- 不再从普通 API call chain 猜 URL；
- 不修改旧 Snapshot，也不读取现场仓库补数据；
- 用户必须重新分析 member 并创建新 View；
- 新 topology schema 和规则身份使旧 cache 自然失效，不迁移旧 registry topology cache；
- 直接删除旧 API、CLI 命令/参数、Web payload 和 MCP 工具契约；
- 发布说明列出所有 breaking changes。

## 21. 最终验收清单

- [x] Graph View 强制单 Scope，member 即 service；
- [x] View 冻结 display name、声明别名和 topology rule identity；
- [x] Repository Snapshot 发布 communication summary + immutable detail artifact；
- [x] 所有通信事实只来自冻结 graph/source；
- [x] 所有服务调用从已识别生产入口可达；
- [x] 相对 URL 无 binding 时不产生 confirmed edge；
- [ ] confirmed、ambiguous、out-of-scope、known external、unresolved 严格分离；
- [x] HTTP/MQ/gRPC 服务边与 Redis/DB 资源边严格分离；
- [x] broadcast 与 competing consumer 语义正确；
- [x] observation、endpoint dependency、service projection 三层可追溯；
- [x] 每条 confirmed edge 有 typed evidence、规则和可解释置信度；
- [x] Partial Topology 明确 coverage 和 unknown boundaries；
- [x] Topology 是唯一异步持久 Artifact；
- [x] Impact/Flow 同步读取同一 Topology，不建立派生 Job；
- [x] Impact 返回 upstream/downstream direct/transitive；
- [x] Flow 返回 rooted graph、branch/merge/cycle/truncation；
- [x] GET 只读、POST 显式创建 Job；
- [x] Job request spec、lease、heartbeat、fencing、retry 和引用释放正确；
- [x] Artifact payload 不含 view ID，交付 envelope 包含请求 view ID；
- [x] Scope 外依赖不进入内部 Service Graph 或继续遍历；
- [x] 旧 Snapshot 不 live fallback，旧公开接口无兼容保留；
- [ ] 不包含实体对齐能力；
- [ ] Tier-1 golden/真实项目准确性门禁通过；
- [x] 删除或移动原仓库后，同一 View 重建出相同 payload digest；
- [x] API、CLI、MCP、Web 使用同一 application service 和 DTO；
- [ ] Artifact/Job retention、LRU/TTL 和授权约束生效；
- [ ] 全量测试、前端构建、服务重启和 `/api/repos` 冒烟验证通过。

## 22. 实施验收记录

当前代码已验证：后端全量 **228** 项测试、Web **17** 项测试、通信事实/拓扑构建/Job fencing/API/CLI
回归测试，以及前端 Vite production build。通信 collector 已接入版本化 Tier-1 支持矩阵、调用路径 coverage、
语义 ID、manifest 边界、payload/Artifact 摘要校验；API/Web/MCP/CLI 均通过同一 Graph Artifact
application service 交付。剩余验收项不是接口缺失，而是部署环境相关工作：

1. 使用 Tier-1 实际项目补齐 proto descriptor、broker binding 和框架特定 collector 的 golden
   accuracy 门禁（规则矩阵和预算已落盘，真实项目标注尚未随仓库提供）；
2. 在部署环境启用管理员身份后再公开 Job cancel（当前共享 API 不暴露 cancel）；
3. 在部署环境运行服务重启、`/api/repos` 冒烟和性能基准，确认数据目录、CAS 清理和线程并发
   参数符合部署配置；当前开发容器已在隔离端口完成构建启动探测，但默认 8765 被外部进程占用。
4. 将当前 `out_of_scope_or_unregistered` 边界状态与部署侧的 known-external registry 对接后，
   再开放严格的 `known_external` 分类门禁；实体对齐仍保持既有独立能力，不属于本拓扑契约。
