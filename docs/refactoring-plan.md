# CodeHistory 重构规划

> 状态：已完成（2026-08-01）  
> 编写日期：2026-08-01  
> 适用版本：`0.1.x` 之后的渐进式重构

实施结果、提交对应关系和验证记录见 [refactoring-result.md](refactoring-result.md)。

## 1. 背景与目标

CodeHistory 已具备演进分析、单仓知识提取、多仓拓扑、CLI、Web API、MCP 和 Web Dashboard 等完整能力。当前主要问题不是功能缺失，而是分析算法、SQLite 查询、文件系统、缓存、LLM 编排和展示格式相互穿透，导致以下风险：

- 大型模块承担过多职责，修改一个维度容易影响其他能力。
- 多个模块重复访问 CodeGraph SQLite，查询语义和连接生命周期不统一。
- CLI、API 和 MCP 重复实现过滤、聚合、缓存与序列化。
- 缺少足够的契约测试，无法证明结构迁移前后行为一致。
- 公共导入路径、CLI 输出、API JSON、MCP schema 和缓存格式均存在兼容压力。

本次重构的目标是：

1. 建立清晰的领域、端口、基础设施、应用和交付边界。
2. 让分析算法依赖抽象接口，而不是 SQLite、Path 或全局状态。
3. 让 CLI、API、MCP 共享应用服务，避免重复业务逻辑。
4. 保留现有公开接口，通过兼容 facade 渐进迁移。
5. 每个阶段独立可测试、可合并、可回滚。

本规划不以减少代码行数为目标，也不在机械重构中同时调整匹配阈值、分析规则或输出语义。

## 2. 当前结构问题

### 2.1 Knowledge Extractor

`knowledge.py` 同时承担：

- 领域 DTO
- 九类确定性知识提取
- CodeGraph SQL 查询
- 图算法
- 文件读取
- LLM 用例编排
- 报告裁剪和序列化

业务代码可通过 `reader.conn` 直接执行 SQL，使 `CodeGraphReader` 无法形成有效抽象边界。

### 2.2 CodeGraph 读取层

`codegraph_reader.py` 混合了：

- SQLite adapter
- Schema 到 DTO 的映射
- 调用图遍历
- 入口点启发式分类
- HTTP 框架规则

调用方需要手工关闭连接，异常路径可能遗漏资源清理。

### 2.3 跨仓分析

`cross_repo.py` 同时负责服务探测、HTTP 调用抽取、源码正则、路径匹配、拓扑构建、影响分析、流程追踪和终端格式化。

`p2_advanced.py` 又重复实现 SQLite 查询，并混合 HTTP/MQ/gRPC 收集、BFS、实体相似度、LLM 验证和文本输出。

### 2.4 Registry 与缓存

`registry.py` 同时承担 JSON CRUD、服务探测、仓库发现、健康检查、拓扑构建与缓存查询。缓存写入不是原子操作，基础设施模块还会反向调用分析器。

### 2.5 交付层

- `cli.py` 混合 parser、命令编排和输出渲染。
- `api.py` 使用全局 Store 缓存，部分路由直接访问 `store.conn`，列表接口存在全量加载和 N+1 查询。
- `mcp_server.py` 重复实现查询与聚合，没有复用 API/CLI 用例。
- Web 页面重复拼接 URL、调用 `fetch` 并吞掉异常。

## 3. 目标架构

```text
codehistory/
├── domain/
│   ├── evolution.py
│   ├── knowledge.py
│   ├── topology.py
│   └── matching.py
├── ports.py
├── infrastructure/
│   ├── codegraph_sqlite.py
│   ├── evolution_sqlite.py
│   ├── registry_json.py
│   ├── topology_cache_json.py
│   └── litellm_client.py
├── analysis/
│   ├── evolution/
│   ├── knowledge/
│   └── topology/
├── application/
│   ├── evolution_service.py
│   ├── knowledge_service.py
│   ├── topology_service.py
│   └── repository_service.py
├── delivery/
│   ├── cli/
│   ├── api/
│   └── mcp/
└── compatibility/
```

依赖方向固定为：

```text
delivery → application → analysis/domain/ports ← infrastructure
```

约束：

- `domain` 不导入 SQLite、Path、FastAPI、FastMCP、litellm。
- `ports` 只定义 Protocol 和输入输出契约。
- `infrastructure` 不编排业务用例。
- `delivery` 不直接执行 SQL 或分析算法。
- composition root 只存在于 CLI、API 和 MCP 启动入口。

## 4. 核心接口设计

### 4.1 CodeGraph Repository

```python
class CodeGraphRepository(Protocol):
    def functions(self) -> list[FunctionDef]: ...
    def call_edges(self) -> list[CallEdge]: ...
    def inbound_endpoints(self) -> list[Endpoint]: ...
    def outbound_calls(self) -> list[OutboundCall]: ...
    def imports(self) -> list[ImportDef]: ...
    def entities(self) -> list[EntityDef]: ...
    def files(self) -> list[FileRecord]: ...
```

`SQLiteCodeGraphRepository` 是唯一允许直接访问 CodeGraph SQLite 的实现，并支持 context manager。

### 4.2 源码读取

```python
class SourceProvider(Protocol):
    def read_text(self, path: str) -> str | None: ...
    def snippet(self, path: str, start: int, end: int) -> str | None: ...
```

仓库根目录必须显式注入，不再从数据库路径隐式推导。

### 4.3 知识提取器

```python
class KnowledgeExtractorPort(Protocol):
    def extract(self, repo: CodeGraphRepository) -> object: ...
```

每个维度独立实现，`KnowledgeReportBuilder` 只负责调用顺序、裁剪和序列化。

### 4.4 跨仓分析

```python
class TopologyBuilder:
    def build(self, repositories: list[RepositoryRef]) -> UnifiedTopology: ...

class ImpactAnalyzer:
    def analyze(self, topology: UnifiedTopology, service: str) -> ImpactResult: ...

class FlowTracer:
    def trace(self, topology: UnifiedTopology, request: FlowRequest) -> Flow: ...

class EntityAligner:
    def align(self, repositories: list[RepositoryRef]) -> CrossServiceEntities: ...
```

路径、topic 和实体相似度规则放入纯函数 matcher，便于表驱动测试。

### 4.5 应用服务

```python
class EvolutionQueryService:
    def list_features(self, filters, page) -> Page[FeatureSummary]: ...
    def get_feature(self, stable_id: str) -> FeatureDetail: ...
    def list_events(self, filters, page) -> Page[EventSummary]: ...

class TopologyService:
    def get_or_build(self, repos, force: bool = False) -> TopologyResult: ...
    def impact(self, service: str) -> ImpactResult: ...
    def trace(self, request: FlowRequest) -> Flow: ...
```

CLI、API 和 MCP 必须复用这些用例。

## 5. 分阶段迁移计划

### 阶段 A：建立行为基线

目标：证明后续重构没有改变公开行为。

任务：

- 为 `KnowledgeExtractor.extract_all()` 建立 golden JSON。
- 固定 CLI 16 个命令的 flags、help、stdout、stderr 和 exit code。
- 建立 API OpenAPI snapshot 和主要响应 contract test。
- 固定 MCP 5 个 tool 的名称、输入 schema 和返回 JSON。
- 建立 HTTP path、MQ topic、实体相似度表驱动测试。
- 建立两到三个微服务 fixture，覆盖 HTTP、MQ、gRPC 和循环拓扑。
- 建立旧 Python import 路径 contract test。

验收：所有现有行为都有自动化基线；本阶段不修改生产逻辑。

### 阶段 B：提取领域模型和 SourceProvider

任务：

- 将 DTO 移到 `domain`，旧模块继续 re-export。
- 提取 `SourceProvider` 和默认文件系统实现。
- 为源码缺失、编码错误、越界 snippet 增加测试。

验收：旧 import、dataclass 字段、相等语义和 JSON 输出不变。

### 阶段 C：统一 CodeGraph Repository

任务：

- 增加 typed query API。
- 将 `knowledge.py` 的 raw SQL 逐项迁入 repository。
- 消除 `cross_repo.py` 和 `p2_advanced.py` 的重复 `_query`。
- 增加 context manager，覆盖异常关闭连接。
- 暂时保留 `CodeGraphReader` facade 和 `.close()`。

验收：调用方不再新增 `reader.conn` 访问；新旧实现对同一 fixture 输出深比较一致。

### 阶段 D：拆分知识提取器

建议顺序：

1. layer rules
2. dependencies、authorization、config
3. API contract、test gaps
4. entities、heatmap、module topology
5. report builder
6. semantic extractors

目标结构：

```text
analysis/knowledge/
├── api_contract.py
├── module_topology.py
├── core_entities.py
├── test_gaps.py
├── layer_rules.py
├── config_usage.py
├── dependencies.py
├── authorization.py
├── heatmap.py
├── semantic.py
└── report_builder.py
```

`KnowledgeExtractor` 保留为兼容 facade。

### 阶段 E：拆分拓扑和流程分析

任务：

- 提取 ServiceInspector、HTTP/MQ/RPC collector。
- 提取 PathMatcher、TopicMatcher、EntitySimilarity。
- 提取 TopologyBuilder、ImpactAnalyzer、FlowTracer、EntityAligner。
- 将 `format_*` 移至 presentation renderer。
- 避免 `P2Analyzer` 为获取 HTTP 边重复执行完整拓扑分析。

验收：`CrossRepoAnalyzer` 和 `P2Analyzer` 仍可按原方式构造，结果与 golden fixture 一致。

### 阶段 F：拆分 Registry 和缓存

任务：

- 提取 RegistryRepository、ServiceDetector、RepositoryDiscovery、HealthChecker。
- 提取 TopologyCache，路径通过构造注入。
- 使用临时文件加 `os.replace()` 原子写 JSON。
- 缓存增加 `schema_version`，兼容读取无版本旧缓存。
- 将拓扑构建编排移到 application service。

验收：现有 `registry.py` 函数继续工作；损坏 JSON 和写入失败不会破坏上一份有效数据。

### 阶段 G：建立共享应用服务

任务：

- 建立 EvolutionQueryService、KnowledgeService、TopologyService。
- 将过滤、分页和统计下推到 repository。
- 消除 API feature 列表 N+1 查询。
- 保证 topology/impact/trace 单次命令最多构建一次拓扑。
- 先让 CLI 使用应用服务，再迁移 API 和 MCP。

### 阶段 H：瘦身交付适配器

CLI：

- parser 使用 `set_defaults(handler=...)` 分派。
- 命令编排和文本 renderer 分离。
- 保持现有命令、参数和退出码。

API：

- 提供 `create_app(dependencies)` 工厂。
- 使用 lifespan 关闭 Store。
- 路由只负责校验、调用用例和 DTO 映射。
- CORS、监听地址和注册能力配置化。
- 保留 `app = create_app()`。

MCP：

- tools 只负责参数映射和 JSON 边界。
- 保持 tool 名称和 schema。

Web：

- 提取统一 API client 和 `useAsync`。
- 统一 HTTP 错误、加载状态和 repo query 编码。
- API 契约稳定后再考虑 UI 组件拆分。

### 阶段 I：隔离 LLM 层

目标结构：

```text
semantic/
├── config.py
├── client.py
├── json_parser.py
├── service.py
├── models.py
└── prompts/
```

任务：

- 定义 `LLMClient.complete()`。
- 提供 public `complete_json()`。
- 将 prompts 与 transport 分离。
- 消除外部模块对 `_call_llm`、`_parse_json` 的依赖。
- 保留 `llm.py` 的现有 public functions 作为 facade。

批处理并发属于行为变化，应在结构迁移完成后单独实现。

## 6. 兼容策略

至少在一个 minor 版本内保留：

- `codehistory.knowledge.KnowledgeExtractor`
- `codehistory.codegraph_reader.CodeGraphReader` 及现有 DTO re-export
- `codehistory.cross_repo.CrossRepoAnalyzer`
- `codehistory.p2_advanced.P2Analyzer`
- `registry.py` 的现有函数
- `api.app` 和 `serve()`
- `mcp_server.run_server()`

兼容要求：

- dataclass 字段和默认值不变。
- `extract_all()` JSON 字段、排序和 cap 不变。
- CLI 命令、参数、输出和退出码不变。
- API 和 MCP 字段不变。
- cache 新增版本字段，但必须兼容读取旧格式。
- 废弃接口先发出 `DeprecationWarning`，不得在同一版本直接移除。

## 7. 测试策略

### Domain

- 路径模板、host、topic、实体相似度表驱动测试。
- 拓扑和 flow 的环、菱形、未知目标与 max depth。
- 图遍历稳定排序。

### Infrastructure

- 临时 CodeGraph SQLite fixture。
- NULL、坏 JSON、schema 缺列和只读连接。
- context manager 正常与异常关闭。
- registry/cache 原子写、损坏恢复和路径注入。

### Application

- fake ports 验证 cache hit、miss、stale、force。
- 验证一次用例只构建一次 topology。
- SQL 分页、过滤、总数和 repo 隔离。
- Store 生命周期关闭。

### Delivery

- CLI golden tests。
- FastAPI TestClient：400、404、分页、repo 隔离、lifespan。
- MCP tool contract tests。
- Web mock fetch：错误、stable_id 编码、repo 切换。

### End-to-End

- 两到三个微仓 fixture。
- HTTP、MQ、gRPC topology 和 entity alignment。
- 旧 facade 与新服务输出深比较。

每个阶段必须通过：

```bash
python -m pytest -q
ruff check codehistory tests
python -m build
cd web && npm run build
```

## 8. 与重构分离的行为问题

以下疑似问题应先建立失败测试，再使用独立提交修复：

1. `get_callers()` 可能返回被调用者名称，而非 caller 名称。
2. `get_call_tree(max_depth)` 当前近似限制节点数，不是真正深度限制。
3. call chain 的 `from` 字段可能填入 callee ID。
4. `batch_explain_functions(max_concurrency)` 参数目前未实际控制并发。
5. P2 文档声称支持 DB flow，但当前流程主要覆盖 HTTP、MQ 和 gRPC。
6. topology、impact、trace 路径可能重复执行拓扑构建。

不得将这些行为修复混入纯移动、重命名或接口提取提交。

## 9. 风险控制

| 风险 | 等级 | 控制措施 |
|------|------|----------|
| 启发式分析结果漂移 | 高 | Golden fixture、新旧实现双跑深比较 |
| 公共 import 破坏 | 高 | Facade、re-export、contract tests |
| CLI/API/MCP 契约变化 | 高 | Snapshot/OpenAPI/tool schema tests |
| 缓存或 registry 不兼容 | 高 | schema version、兼容读、原子写 |
| SQL 列、排序或 cap 遗漏 | 高 | Typed repository contract + golden JSON |
| SQLite 生命周期和线程问题 | 中 | Connection factory、context manager、lifespan |
| 拆分后循环依赖 | 中 | 固定依赖方向，composition root 集中 |
| LLM prompt 意外变化 | 中 | Prompt snapshot、fake client、独立提交 |

## 10. 建议提交序列

1. `test: add characterization coverage for public contracts`
2. `refactor: extract domain models and source provider`
3. `refactor: introduce codegraph repository`
4. `refactor: split knowledge extractors`
5. `refactor: split topology and flow analysis`
6. `refactor: isolate registry and cache infrastructure`
7. `refactor: add shared application services`
8. `refactor: modularize CLI API and MCP adapters`
9. `refactor: isolate semantic LLM services`
10. 独立提交经过测试确认的行为修复

## 11. 完成定义

重构完成需同时满足：

- 分析算法不直接依赖 SQLite、Path、FastAPI、FastMCP 或 litellm。
- CodeGraph SQLite 仅通过 repository adapter 访问。
- CLI、API、MCP 共享 application services。
- 旧公开导入和运行入口在兼容周期内有效。
- 所有 golden、contract、integration 和 build 验证通过。
- 现有缓存和 registry 数据可无损读取。
- 大型 facade 仅保留委托逻辑，不再包含核心算法。
- 文档架构、实现结构和公开能力保持一致。
