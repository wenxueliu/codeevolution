# CodeEvolution — 代码仓功能演进分析 + 业务知识逆向系统设计文档

> 下一阶段已确认方案：[仓库级解析快照设计](repository-analysis-snapshot-design.md)。该方案尚未实施，
> 将以用户手动生成的仓库级时间点快照替代实时读取，并移除旧 Git Evolution Engine。

## 1. 项目定位

CodeEvolution 做两件事：

1. **从代码逆向业务知识** — 基于 CodeGraph 知识图谱，无需重新解析代码，自动抽取 18 维结构化知识
2. **追踪代码演进** — 沿 git 历史逐 commit 分析，以"功能"为单位追踪代码的演变过程

服务产品经理、架构师、开发、测试、运维五个角色。输出形态为 CLI + MCP Server + Web 控制台。

### 1.1 与现有工具的差异

| 工具 | 做什么 | CodeEvolution 增加什么 |
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
  Phase 1: 纯图计算 + 路径骨架（PageRank/BFS/构建清单）→ 9 维
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
codeevolution/
  domain/                # 纯 DTO
  ports.py               # Repository / SourceProvider Protocol
  infrastructure/       # SQLite、源码、Registry、缓存 adapter
  analysis/
    knowledge/           # 独立知识维度 + report builder
    topology/            # topology / impact / flow / matcher
  application/           # Evolution / Knowledge / Topology / Repository 用例
  delivery/              # renderer 与交付适配边界
  semantic/              # LLM config / client / parser / models / service

  engine.py              # 演进引擎
  walker.py / store.py   # Git 遍历与 Evolution SQLite
  analyzer.py / matcher.py

  codegraph_reader.py    # 兼容 facade
  knowledge.py           # 兼容 facade
  cross_repo.py          # 兼容 facade
  p2_advanced.py         # 兼容 facade
  registry.py / llm.py   # 兼容函数入口
  cli.py / api.py / mcp_server.py
```

实施明细与验证记录见 [refactoring-result.md](refactoring-result.md)。

---

## 4. 知识提取：三阶段 13 维

### Phase 1 — 纯图计算（秒级，无 LLM）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 1 | API 契约 | route nodes + handler functions | HTTP decorator 匹配 + 签名提取 |
| 2 | ADR 决策记录 | 仓库 `docs/adr` 等目录的 Markdown | MADR 4.0 frontmatter 解析 + R1/R2 结构校验 |
| 3 | 模块拓扑 | imports/calls edges + 构建清单/目录布局 | 路径骨架 + 图耦合（依赖/耦合度/内聚度） |
| 4 | 核心实体 | calls edges | PageRank 中心度 |
| 5 | 分层违规 | `layers.json` + calls edges | 仓库声明的 glob 分层 + 允许/禁止边 |

### Phase 2 — 图 + 规则（秒级，无 LLM）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 6 | 配置消费 | config files + variable nodes | key 名匹配 + 调用链追踪 |
| 7 | 外部依赖 | import nodes + decorators | 40+ 已知服务模式匹配 |
| 8 | 权限模型 | decorators + function names | 20+ 权限注解模式匹配 |
| 9 | 热力图 | calls edges | in/out-degree 百分位分层 |

#### 分层声明（`layers/v1`）

分层是仓库属性，不是语言属性：Java MVC、Python 六边形和 Go module 布局用不同路径
约定表达同一批关注点，任何内置表都只能覆盖其中一种。因此分层改为仓库数据：

```json
{
  "version": "layers/v1",
  "layers": {"domain": ["**/domain/**"], "infrastructure": ["**/infrastructure/**"]},
  "rules": {"strict": true, "allow": [["domain", "infrastructure"]], "deny": [], "ignore": ["test"]}
}
```

- 位置：`<repo>/.codeevolution/layers.json`，随仓库提交，可 review。
- 匹配：glob 对仓库相对路径，大小写敏感；字面字符更多者优先，其次更长的模式，再次声明顺序。
- 判定：`deny` 优先，其次 `allow`，未命中时由 `strict` 决定（严格模式下未声明即违规）。
  `ignore` 中的层（默认 `test`）不参与判定。
- 缺省：仓库没有配置文件时回落到内置表，行为与旧版目录约定检查完全一致
  （`strict: false` + 6 条显式 `deny`），升级不改变既有结论。

`layers init` 从现有代码图反推声明：角色目录（`**/service/**`）优先，文件名后缀族
（`**/*Controller.*`，要求跨至少 3 个文件、2 个目录才算约定）兜底，再把**当前已存在**
的每一对依赖方向写成 `allow`。生成的配置必然通过它自己，之后再逐对收紧。

`layers check` 消费同一份声明，配合 `layers.baseline.json` 支持存量治理：先
`--write-baseline` 把现有违规计入基线，此后只有新增违规让 CI 失败。

Web 知识中心的「分层违规」分区内嵌「分层规则」面板：每个仓库一份声明，面板随当前
Snapshot 所属仓库定位 `<repo>/.codeevolution/layers.json`，提供在线查看、编辑、重新
生成、检查和写基线。检查与生成针对该 Snapshot 的冻结代码图执行，因此预览结果与页面上
列出的违规同源；保存声明后需重新分析生成新 Snapshot 才会改变已发布事实。接口为
`GET|PUT /api/repository-snapshots/{snapshot_id}/layers`、
`POST /api/repository-snapshots/{snapshot_id}/layers/{init,check,baseline}`。
分析发布时读取仓库声明并写入 facts，声明内容的摘要并入 `rules_digest`，声明变化会产生
新 Snapshot。

#### ADR 决策记录（`adr/v1`）

架构决策记录属于仓库数据，和分层声明一样从 checkout 读取：分析发布时依次探测
`docs/adr`、`doc/adr`、`docs/decisions`、`adr`，解析 MADR 4.0 frontmatter
（`status` / `date` / `decision-makers` / `supersedes`），并兼容旧模板的 `## Status` 章节。
校验沿用 ADR 工程化规则的 R1（文件名、编号唯一与连续性、标题、状态、日期、模板占位符、
`## Context and Problem Statement` / `## Decision Outcome` 必需章节）和 R2（替代目标存在、
双向链接、环检测）。记录随 facts 冻结在 Snapshot 中；旧 Snapshot 无此事实时返回
`available: false`。生成、索引、索引同步与 CI 门禁仍由 `sw-adr-engineering` 技能负责，
CodeEvolution 只读不改写决策记录。

### Phase 3 — 图 + LLM（需要 API key）

| # | 维度 | 输入 | 算法 |
|---|------|------|------|
| 10 | 业务描述 | function signature + callers/callees + 源码 | LLM → 中英文摘要 + 业务域分类 |
| 11 | 业务规则 | 函数体源码 | LLM → 验证/转换/授权/工作流规则提取 |
| 12 | 错误目录 | 函数体源码 | LLM → 错误类型+触发条件+处理策略 |
| 13 | 状态机 | enum nodes + 使用函数 | LLM → 状态+转换+触发器推导 |

API 功能解释采用人工触发、叶子优先的节点解释与快照发布机制，详细设计见
[API 功能解释与快照设计](api-explanation-snapshot-design.md)。

---

### 4.1 ADR 生成子系统（模型驱动）

ADR 不只是被读取的事实，核心是**用模型生成**：从 git 提交、文件、一句话描述或历史批量
生成 MADR 4.0 决策草稿，落库后由 CLI/API 返回，客户端再写入代码仓的 `docs/adr`。

- **输入**：`commit`（提交信息 + 变更文件 + patch，diff 截断 60k 字符）、`file`（仓库
  内路径或客户端上传的文件内容，截断 120k 字符）、`text`（一句话描述，截断 20k 字符）、
  `history`（按时间倒序的提交批量，`--since/--until/--limit`）。提交输入默认过滤
  docs/tests/lockfile 等噪声提交和 merge commit，`force` 可绕过。
- **提示词**：内置 `commit`/`file`/`text` 三套默认模板，按**服务（repository member）
  × 来源类型**版本化覆盖；占位符为 `{{service}} {{repo}} {{source_ref}} {{commit_message}}
  {{commit_files}} {{diff}} {{content}} {{text}} {{schema}} {{existing_adrs}}`。
  每次生成冻结 `prompt_id/version/digest/text`，提示词改动不会污染历史记录。
- **存储**：`analysis_data_dir()/adr.db`，四张表：`adr_prompts`（版本化提示词）、
  `adr_generations`（生成任务与调试轨迹）、`adr_drafts`（MADR 草稿与
  `revision/editor/reviewer` 元数据）、`adr_draft_revisions`（每次保存的全文版本）。
  状态机：`queued → running → succeeded | failed`；模型判定「不是架构决策」时为
  `skipped`。生成状态一律 `proposed`，只有人能改为 `accepted`。
- **调试映射**：一次 generation 完整记录 `source_payload`（提交 diff / 文件内容 / 文本）、
  来源摘要、实际提示词全文与摘要、模型名、原始响应、解析后的决策对象、错误或跳过原因、
  耗时。`adr debug <generation-id>` 与 `GET /api/adr/generations/{id}` 暴露同一份映射。
- **提交表格与 AI 分诊**：`adr commits` 分页展示**整个 git 历史**（`--offset/--limit`，
  默认每页 50、上限 100），规则层先过滤文档/测试/lockfile 噪声并由「提交分诊」提示词按
  当前页判断 bug fix、lint、test-only 等是否值得记录；判定缓存到 `adr_commit_scans`
  （按 `member + sha`），表格支持单选/勾选批量生成
  （`adr gen --commits`、`POST /api/adr/generations/batch`）。`force` 可绕过分诊与噪声过滤。
  每个 commit 投影生命周期状态：`pending`（待生成）、`queued`、`generating`（生成中）、
  `generated`（待校验）、`backtested`（待审批）、`approved`/`rejected`、`exported`、
  `skipped`、`failed`；`GET /api/adr/commits` 同时返回各状态计数、按整段历史统计的已处理
  进度（processed/total/percent）、`offset/total_all` 分页字段与 `stage` 过滤参数。
  历史整段索引在一次 `git log --name-only` 中读取并按 HEAD 缓存 30s；超过 20000 个提交时
  返回 400 提示用 `since/until` 缩小范围，而不是给出只覆盖部分历史的进度。
- **输出模板**：默认内置 MADR 4.0（`{{frontmatter}} {{title}} {{context}} {{drivers}}
  {{options}} {{outcome}} {{consequences}} {{confirmation}} {{options_detail}}
  {{evidence}} {{service}} {{date}} {{status}}`），按服务版本化覆盖；`{{title}}` 必填。
- **回测**：`adr backtest <draft-id>` 用「回测」提示词把 ADR 与当前代码证据（ADR 引用的
  文件内容、仓库文件清单、最近提交）交给模型，输出 verdict（consistent/partial/
  contradicted/unverifiable）、confidence、理由与证据，存入 `adr_backtests`。
- **人工审批**：`adr review <draft-id> --approve|--reject [--rating 1-5] [--comment]
  [--reviewer]` 写入 `review_status`、评分、意见与审批人，并把 MADR frontmatter 状态改为
  accepted/rejected；只有 approved 的草稿才允许导出，否则返回 409。
- **精修与版本**：`adr edit <draft-id> --file|--text [--title] [--editor] [--note]`
  （`PUT /api/adr/drafts/{id}`）把人工/外部 agent 改写的 MADR 保存为**新版本**：追加
  `adr_draft_revisions`、`revision + 1`、frontmatter 状态重置为 proposed、审批状态重置为
  pending、旧回测结果删除（需重新回测）。已导出（`exported_path`）的草稿不可再编辑或
  重新审批，返回 409——仓库文件即该决策的权威文本。
- **导出**：`adr export <draft-id>` 或 `POST /api/adr/drafts/{id}/export` 写入仓库
  `docs/adr`（已有 `doc/adr`/`docs/decisions`/`adr` 则复用），按现有编号续号、
  slug 化文件名、重建 `index.md`；重复导出同一草稿复用原路径，不产生重复文件。
  导出时在 frontmatter 注入 `provenance:` 块（`draft_id`、`generation_id`、`member_id`、
  `source`、`model`、`prompt_digest`、`revision`、`evidence`、`editor`、`reviewer`、
  `reviewed_at`、`exported_at`、`tool`），且重导出只替换该块、不重复追加。这是**交付物
  契约**：仓库只保存经过人工确认、可溯源的记录；生成内容永不作为后续分析的证据。
- **与快照的关系**：导出的 Markdown 是仓库数据，只有重新分析生成 Snapshot 后才会进入
  `-s adr` 的快照事实；ADR 内容摘要并入 `rules_digest`，决策变更会产生新 Snapshot。

## 5. 多仓微服务分析：三阶段 5 维

### P0 — 跨服务调用拼接

从每个服务提取 HTTP 路由和消息消费者入口，以处理函数为根沿 CodeGraph `calls` 边遍历，只保留入口可达的 HTTP、消息发布和 Redis 调用。HTTP 通过服务 host、方法和路径模板拼接；Kafka、RabbitMQ、NATS、Redis Pub/Sub 与 Redis Streams 通过消息通道拼接生产者和消费者。先生成带完整证据链的 typed endpoint edges，再投影为服务级依赖图。

```
order-svc:  requests.post("http://user-svc/api/users/123")  → 出站调用
user-svc:   POST /api/users/:id                               → 入站 API

端点级结果:
  order-svc::POST /orders
    → create_order → validate_user → HTTP client
    → user-svc::POST /api/users/:id

服务级投影:
  order-svc → user-svc
```

每条高置信依赖边保存入口端点、内部调用路径、HTTP 调用位置、原始 URL、目标处理函数和匹配规则。相同方法与路径命中多个服务时不按遍历顺序选择，而是保留为歧义候选；无法匹配已注册服务的 host 作为外部依赖候选。仓库中存在但不能从入口端点到达的 HTTP 调用不会进入 API 服务依赖图。

消息边保存 broker 类型、topic/queue/channel/stream、生产函数、消费函数、入口类型和完整调用链。只有可从 HTTP 或消息消费入口到达、且通道名高置信匹配的发布调用才进入服务依赖图；未解析通道不会宽泛匹配，避免制造异步依赖。

Redis 采用两种不同语义：`PUBLISH/SUBSCRIBE` 和 `XADD/XREADGROUP` 通过通道形成异步服务依赖，同时保留服务到 Redis 实例的底层资源边；`GET/SET/HGET/HSET` 等缓存或数据命令只形成资源依赖。资源依赖写入独立的 `resource_edges` 与 `resource_dependency_graph`，不会把共享缓存误判成服务互调。实例标识优先取脱敏后的 Redis URL，其次取 host/port，无法解析时使用 `redis:default`；每条边同时保留命令、key、入口和 CodeGraph 调用链。

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

CodeEvolution 不需要自己实现解析 —— 只需要读 CodeGraph 的 SQLite。

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
| 多仓分析 | cross_repo.py（P0） | 完成：HTTP/MQ/Redis typed edges + 双层依赖图 + 影响分析 |
| 多仓分析 | registry.py（P1） | 完成：服务发现 + 自动检测 + 健康检查 + 拓扑缓存 |
| 多仓分析 | p2_advanced.py（P2） | 完成：全通道流程追踪 + 跨服务实体对齐 |
| 演进引擎 | engine.py | 完成：git checkout + codegraph sync + 特征追踪 |
| 演进引擎 | walker.py / store.py / analyzer.py / matcher.py | 完成 |
| CLI | cli.py | 完成：18 个子命令 |
| Web | api.py / web/ | 完成：Vue 3 + Mermaid 时序图 |
| MCP | mcp_server.py | 完成：5 个 MCP tools |
| 文档 | CLAUDE.md / README.md / INSTALL.md / design.md | 完成 |
| 技能 | codeevolution-knowledge / codeevolution-exploring | 完成 |

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
