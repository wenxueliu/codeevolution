# CodeHistory — 代码仓功能演进分析 + 业务知识逆向系统设计文档

## 1. 项目定位

CodeHistory 做两件事：

1. **从代码逆向业务知识** — 基于 CodeGraph 知识图谱，无需重新解析代码，自动抽取 18 维结构化知识
2. **追踪代码演进** — 沿 git 历史逐 commit 分析，以"功能"为单位追踪代码的演变过程

服务产品经理、架构师、开发、测试、运维五个角色。输出形态为 CLI + MCP Server + Web Dashboard。

### 1.1 与现有工具的差异

| 工具 | 做什么 | CodeHistory 增加什么 |
|------|--------|---------------------|
| `codegraph` (npm) | 对当前代码建知识图谱 | 从图谱逆向业务知识（18 维）、跨仓库拼接、时间维度演进追踪 |
| `code-review-graph` (PyPI) | PR diff 影响分析 | 长期演进趋势、业务规则提取、跨服务实体对齐 |
| OpenTelemetry / Sentry | 运行时 trace/error | 静态知识图谱 + 代码结构关联（Phase 3+） |

---

## 2. 核心定义

### 2.1 知识提取

```
知识 = 从 CodeGraph 知识图谱中推导的结构化信息

三层递进:
  Phase 1: 纯图计算（PageRank/Louvain/BFS）→ 9 维
  Phase 2: 图 + 规则引擎（模式匹配）→ 4 维
  Phase 3: 图 + LLM 语义理解（litellm）→ 4 维

所有层的输入都是 CodeGraph 的 SQLite 三张表（nodes/edges/files）。
不需要重新解析代码。
```

### 2.2 演进追踪

```
功能 = 入口点 + 下游调用树

入口点 = API endpoint / CLI command / event handler / cron job
调用树 = 从入口点出发，沿 CALLS 边 BFS 可达的所有符号
演变   = 同一入口点的调用树在时间轴上的结构变化
```

### 2.3 为什么以"功能"为追踪单位

- 符号级追踪的最大瓶颈是 Identity Matcher——函数重命名/拆分/合并准确率无法保证
- 入口点变更频率远低于内部函数，框架装饰器提供了稳定的识别锚点
- 功能级匹配比符号级更可操作：同一 endpoint path + HTTP method 在 95% 以上的情况下就是同一个功能

---

## 3. 系统架构

### 3.0 分层边界（0.2 重构后）

生产代码按固定依赖方向组织：

```text
delivery → application → analysis/domain/ports ← infrastructure
```

- `domain/` 保存纯 DTO；旧模块仅为兼容导入路径 re-export。
- `ports.py` 定义 CodeGraph repository 与源码读取契约。
- `infrastructure/` 保存 CodeGraph SQLite、源码、registry 与版本化缓存 adapter。
- `analysis/knowledge/` 和 `analysis/topology/` 保存可组合分析步骤与纯匹配规则。
- `application/` 提供 Evolution、Knowledge、Topology 与 Repository 共享用例。
- `semantic/` 隔离 LLM 配置、client、JSON parser、模型与 prompt。
- CLI、API、MCP 继续保留公开入口，通过兼容 facade 渐进迁移。

```
                         ┌───────────────────────┐
                         │    CodeGraph SQLite     │
                         │  (nodes / edges / files) │
                         └───────────┬─────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │  Knowledge       │   │  Cross-Repo      │   │  Evolution       │
    │  Extractor       │   │  Analyzer         │   │  Engine          │
    │                  │   │                  │   │                  │
    │  Phase 1: 图计算  │   │  P0: HTTP 调用拼接 │   │  git log 遍历      │
    │  Phase 2: 规则匹配 │   │  P1: 服务发现+缓存  │   │  入口点检测        │
    │  Phase 3: LLM 理解 │   │  P2: 全通道流程+实体 │   │  功能匹配+演变分析  │
    └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
             │                     │                     │
             └─────────────────────┼─────────────────────┘
                                   │
                        ┌──────────┴──────────┐
                        │  CLI / MCP / Web API │
                        └─────────────────────┘
```

### 3.1 模块架构

```
codehistory/
  codegraph_reader.py    # CodeGraph SQLite 读取层（FunctionDef / CallTarget / EntryPointDef）

  knowledge.py           # 单仓知识提取（Phase 1-3，13 维）
  cross_repo.py          # 多仓拓扑（HTTP 调用拼接 + URL 源码提取 + 影响分析）
  p2_advanced.py         # 全通道流程追踪（HTTP+MQ+gRPC）+ 跨服务实体对齐
  llm.py                 # LLM 调用层（litellm / OpenAI / Anthropic）

  registry.py            # 多仓注册中心 + 自动检测 + 服务发现 + 健康检查 + 拓扑缓存

  engine.py              # 演进引擎（git checkout + codegraph sync + 特征追踪）
  walker.py              # Git 历史遍历（git show 读文件对象）
  store.py               # Evolution 事件存储（SQLite WAL）
  analyzer.py            # 快照比较 → 生成演进事件
  matcher.py             # L1 精确匹配 (entry_type, entry_signature) → 特征身份

  api.py                 # FastAPI 后端（多仓 Web Dashboard）
  cli.py                 # CLI 入口（18 个子命令）
  mcp_server.py          # MCP Server（stdio/SSE/streamable-http）
  config.py              # 配置管理
```

---

## 4. 知识提取：三阶段 13 维

### Phase 1 — 纯图计算（秒级，无 LLM）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 1 | API 契约 | route nodes + handler functions | HTTP decorator 匹配 + 签名提取 |
| 2 | 模块拓扑 | imports/calls edges | Louvain 社区检测 |
| 3 | 核心实体 | calls edges | PageRank 中心度 |
| 4 | 测试缺口 | test files + calls edges | 测试→生产 调用差集 |
| 5 | 分层违规 | file_path + calls edges | 目录命名约定检查 |

### Phase 2 — 图 + 规则（秒级，无 LLM）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 6 | 配置消费 | config files + variable nodes | key 名匹配 + 调用链追踪 |
| 7 | 外部依赖 | import nodes + decorators | 40+ 已知服务模式匹配 |
| 8 | 权限模型 | decorators + function names | 20+ 权限注解模式匹配 |
| 9 | 热力图 | calls edges | in/out-degree 百分位分层 |

### Phase 3 — 图 + LLM（需要 API key）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 10 | 业务描述 | function signature + callers/callees + 源码 | LLM → 中英文摘要 + 业务域分类 |
| 11 | 业务规则 | 函数体源码 | LLM → 验证/转换/授权/工作流规则提取 |
| 12 | 错误目录 | 函数体源码 | LLM → 错误类型+触发条件+处理策略 |
| 13 | 状态机 | enum nodes + 使用函数 | LLM → 状态+转换+触发器推导 |

---

## 5. 多仓微服务分析：三阶段 5 维

### P0 — 跨服务调用拼接

从每个服务的 CodeGraph 提取出站 HTTP 调用 + 入站 API 契约 → URL 模式匹配拼接跨服务边。

```
order-svc:  requests.post("http://user-svc/api/users/123")  → 出站调用
user-svc:   POST /api/users/:id                               → 入站 API

匹配结果: order-svc.create_order ──[POST /api/users/:id]──→ user-svc.get_user
```

### P1 — 服务管理

- 注册时自动检测：语言、角色（gateway/backend/worker/cron/frontend）、数据库类型、消息队列类型
- 服务发现：`discover -d /repos` 扫描目录树找 git 仓库
- 健康检查：`check` 验证 CodeGraph 初始化+索引新鲜度
- 拓扑缓存：首次 `topology` 构建后缓存，后续 `impact`/`trace` 秒级返回

### P2 — 高级分析

- 全通道流程追踪：HTTP + Kafka/RabbitMQ/NATS/gRPC，BFS 多跳
- 跨服务实体对齐：CamelCase 分词 + 后缀标准化（Service↔Svc, Repository↔Repo）+ LLM 验证

---

## 6. 演进引擎

### 6.1 核心流程

```
git log --first-parent (按时间正序，每次一个 commit)
       │
       ▼
  1. git checkout <commit>
  2. codegraph sync         ← 增量更新 CodeGraph 索引（仅变更文件）
  3. 查询 CodeGraph SQLite  ← 获取入口点 + 调用树
  4. Feature Matcher        ← 匹配已有功能
  5. Evolution Analyzer     ← 对比快照，生成事件
  6. 写入 EvolutionStore    ← 仅写有变化的 snapshot/event
```

### 6.2 功能匹配策略

```
L1 (高置信度, >0.9): 入口点签名精确匹配
  - HTTP: endpoint path + HTTP method
  - CLI: command name + subcommand chain
  - Event: topic/queue name + handler pattern

L2 (中置信度, 0.6-0.9): 调用树结构相似度（待实现）
L3 (低置信度, <0.6): 内容指纹匹配（待实现）
```

### 6.3 数据模型

```sql
commits (id, hash, parent_hash, timestamp, author, message, semantic_type, tags)
features (id, stable_id, canonical_name, entry_type, entry_signature, first_seen_at, last_seen_at, status)
feature_snapshots (id, feature_id, commit_id, call_tree_nodes, call_tree_edges, call_tree_depth, file_path, line_start, line_end, call_chain)
evolution_events (id, feature_id, commit_id, event_type, detail)
```

---

## 7. CodeGraph 依赖

### 7.1 为什么委托给 CodeGraph

- 30+ 语言 tree-sitter 解析（WASM + Rust kernel 双路径）
- 跨文件 import/call/type 解析已内置
- SQLite 存储，零服务进程依赖
- 文件监听增量更新

CodeHistory 不需要自己实现解析 —— 只需要读 CodeGraph 的 SQLite。

### 7.2 读取的 CodeGraph Schema

```sql
nodes (id, kind, name, qualified_name, file_path, language,
       start_line, end_line, signature, visibility,
       is_exported, is_async, is_static,
       decorators, type_parameters, updated_at)

edges (id, source, target, kind, metadata, line, col, provenance)
  -- kind: contains | calls | imports | exports | extends | implements |
  --        references | type_of | returns | instantiates | overrides | decorates

files (path, content_hash, language, size, modified_at, indexed_at, node_count)
```

---

## 8. 实现状态

### 已完成（2026-07-31）

| 子系统 | 模块 | 状态 |
|--------|------|------|
| 知识提取 | codegraph_reader.py | 完成：CodeGraph SQLite 读取层 |
| 知识提取 | knowledge.py（Phase 1） | 完成：5 维（API/模块/实体/测试/分层） |
| 知识提取 | knowledge.py（Phase 2） | 完成：4 维（配置/依赖/权限/热力图） |
| 知识提取 | llm.py（Phase 3） | 完成：4 维 LLM 语义理解 |
| 多仓分析 | cross_repo.py（P0） | 完成：HTTP 调用拼接 + 统一拓扑 + 影响分析 |
| 多仓分析 | registry.py（P1） | 完成：服务发现 + 自动检测 + 健康检查 + 拓扑缓存 |
| 多仓分析 | p2_advanced.py（P2） | 完成：全通道流程追踪 + 跨服务实体对齐 |
| 演进引擎 | engine.py | 完成：git checkout + codegraph sync + 特征追踪 |
| 演进引擎 | walker.py / store.py / analyzer.py / matcher.py | 完成 |
| CLI | cli.py | 完成：18 个子命令 |
| Web | api.py / web/ | 完成：Vue 3 + Mermaid 时序图 |
| MCP | mcp_server.py | 完成：5 个 MCP tools |
| 文档 | CLAUDE.md / README.md / INSTALL.md / design.md | 完成 |
| 技能 | codehistory-knowledge / codehistory-exploring | 完成 |

### 待实现

| 功能 | 优先级 | 说明 |
|------|--------|------|
| 真实多仓项目验证 | P0 | 需要 2-3 个有跨服务调用的代码仓 |
| OTel 运行时数据接入 | P1 | trace/log/metric 关联到静态图 |
| L2 模糊匹配 | 中 | 调用树图编辑距离匹配 |
| Web 注册界面 | 低 | 前端直接注册 |
| 报告导出 | 低 | PDF/Markdown |

---

## 9. 参考项目

| 项目 | 位置 | 复用 |
|------|------|------|
| codegraph | `reference/codegraph/` | 代码解析引擎（tree-sitter WASM + Rust kernel），SQLite 知识图谱 |
| gitnexus | `reference/gitnexus/` | 多语言解析架构参考，scope-resolution 管线设计 |
| code-review-graph | `reference/code-review-graph/` | git diff 解析，风险评分模型，Leiden 社区检测 |
