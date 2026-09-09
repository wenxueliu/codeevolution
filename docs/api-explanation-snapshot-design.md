# API 功能解释与快照设计

> 状态：设计草案
> 范围：API 调用链节点解释、聚合解释、手动生成与解释快照
> 依赖：CodeGraph、现有 API 契约、`CallTreeService`、`NodeRuleService`

## 1. 背景

CodeEvolution 已能从 CodeGraph 提取 API 端点、调用链和函数源码，并支持人工对调用链节点生成业务规则解释。现有方式适合单点查看，但还不能稳定生成完整、可核查的 API 功能解释：入口函数可能只是简单转发，核心逻辑位于深层调用节点；超大函数可能被源码行数上限截断；API 级提示词仅依赖时序图时，也容易遗漏分支、异常、状态变化和副作用。

本文定义一套统一的解释流水线：遍历 API 可达的全部业务调用节点，从叶子节点开始解释；超大函数先分块；每个节点同时保存自身解释和包含子节点语义的聚合解释；入口节点的聚合解释即 API 功能解释。模型生成只允许人工触发，每次成功生成一个端点级、不可变、可删除的解释快照。

### 1.1 当前实现基线

- `CallTreeService` 已支持按节点懒加载 API 调用树。
- `NodeRuleService` 已支持对单个调用链节点生成功能解释。
- `NodeRuleStore` 当前按节点键 upsert，重新生成会覆盖旧结果。
- 当前节点源码最多读取 120 行，超大函数可能丢失后续源码。
- 单节点生成使用固定 `max_tokens=1200`，尚未根据分块数量和聚合输入动态分配。
- 当前没有端点级解释快照、叶子优先调度、完整性覆盖清单和跨快照增量复用。

## 2. 目标与非目标

### 2.1 目标

- 遍历每个 API 可达的全部仓内业务节点，不因入口或中间节点简单而停止向下分析。
- 每个节点都具有独立、可人工核查的解释和源码证据。
- 超大函数按逻辑块解释，禁止静默截断源码。
- 按叶子到入口的顺序聚合，使父节点解释可以利用全部子节点解释。
- 同时服务人类渐进阅读和机器结构化消费。
- 代码变化只标记当前快照可能过期，不自动调用模型。
- 用户手动触发后创建新快照；生成期间旧快照继续可用。
- 支持删除任意解释快照，并安全处理当前快照删除。
- 手动生成时复用内容未变化的节点，减少模型调用和生成时间。

### 2.2 非目标

- 不展示单个节点解释的历史版本和差异。
- 不在文件保存、Git 更新、CodeGraph 同步或页面刷新时自动调用模型。
- 不解释标准库、第三方库或无法获取源码的外部系统内部实现。
- 不保证静态调用图能还原反射、动态分派和运行时配置产生的所有路径；无法确认的部分必须显式标记。

## 3. 核心定义

```text
解释快照       = 一次人工触发为一个 API 端点生成的调用图、块级解释、节点解释和 API 解释的一致集合
代码块解释     = 一个函数内具有明确行号范围的局部业务事实
节点自身解释   = 仅根据当前函数源码得出的业务解释
节点聚合解释   = 当前节点自身解释 + 调用边语境 + 全部直接子节点聚合解释
API 功能解释   = API 入口节点的聚合解释
当前快照       = 每个 API 端点由页面和查询接口默认读取的已发布快照
过期           = 当前源码或调用图与当前快照记录的摘要不一致，但系统尚未重新生成
```

复杂度只决定函数是否分块、解释预算和聚合层级，不决定是否继续遍历调用链。

## 4. 总体流程

```text
用户对一个 API 点击“生成解释”
        │
        ▼
固化源码清单与调用图
        │
        ▼
计算 SCC 并生成无环依赖图
        │
        ▼
叶子优先处理每个节点
  ├─ 小函数：直接生成节点自身解释
  └─ 大函数：代码块解释 → 节点自身解释
        │
        ▼
结合调用边和子节点解释生成节点聚合解释
        │
        ▼
入口节点聚合为 API 功能解释
        │
        ▼
覆盖校验、记录缺失和失败节点
        │
        ▼
原子发布为该 API 的当前快照
```

生成期间该 API 的当前快照不变。只有候选快照达到发布条件后，系统才切换该 API 的当前快照指针。批量生成必须由用户显式触发；批量任务只是多个端点级生成任务的编排容器，每个 API 仍独立生成和发布快照。

## 5. 调用图构建

### 5.1 遍历边界

从每个 API handler 开始沿 CodeGraph `calls` 边遍历，纳入：

- 当前仓库中可解析源码的函数和方法；
- 已注册服务中能够高置信匹配的下游 API handler；
- 数据库、消息、缓存和外部 HTTP 调用的边界节点。

以下对象不进入源码解释，但保留边界说明：

- 标准库和第三方库；
- 无源码节点；
- 无法匹配目标服务的外部调用；
- 动态调用、反射或配置驱动调用的未解析目标。

任何深度或节点数量保护上限都必须产生 `partial` 状态和明确的截断原因，不能静默丢弃。

### 5.2 共享节点

调用图按稳定节点键去重。同一 Service 函数被多个 API 调用时只生成一份节点自身解释；不同父节点通过调用边记录各自的调用语境。节点键至少包含物理仓库成员和 CodeGraph 节点标识，源码哈希用于判断内容是否变化。

### 5.3 循环调用

递归和循环依赖会使原始调用图不满足拓扑排序条件。系统先用 Tarjan 或 Kosaraju 算法计算强连通分量（SCC），将同一循环中的节点压缩为一个聚合单元，再对压缩后的 DAG 执行反向拓扑排序。

循环分量内仍保留每个节点的自身解释；分量级聚合解释说明循环目的、退出条件、重试上限和异常路径。

## 6. 函数分块与节点自身解释

### 6.1 分块触发

满足任一条件时进入分块流程：

- 源码超过单次安全输入预算；
- 函数包含较多分支、循环或异常处理；
- 模型上下文窗口不足以同时容纳源码、提示词和预期输出；
- 源码读取不完整。

不再使用“读取前 120 行后截断”的方式作为最终解释输入。

### 6.2 分块边界

优先使用语法结构或 CodeGraph 节点信息划分：

- 参数校验和 guard clauses；
- `if/else`、`switch/match` 分支；
- 循环；
- `try/catch/finally`；
- 数据库和事务操作；
- 外部调用和消息发送；
- 状态更新；
- 返回值构造和异常出口。

无法取得结构边界时，才使用带少量重叠上下文的行数窗口兜底。每个代码块必须记录 `line_start`、`line_end` 和 `source_hash`。

### 6.3 块级输出

```json
{
  "chunk_id": "OrderService.createOrder#3",
  "line_start": 86,
  "line_end": 121,
  "summary": "检查并预占商品库存",
  "business_rules": [
    {
      "text": "任一商品库存不足时终止订单创建",
      "evidence_lines": [94, 101]
    }
  ],
  "state_changes": [],
  "side_effects": ["预占库存"],
  "exceptions": ["库存不足"],
  "status": "completed"
}
```

### 6.4 节点自身输出

块级事实去重后生成节点自身解释：

```json
{
  "node_key": "mall::OrderService.createOrder",
  "file": "service/OrderService.java",
  "line_start": 42,
  "line_end": 138,
  "summary": "校验订单并协调价格、库存和持久化流程",
  "business_flow": [],
  "business_rules": [],
  "state_changes": [],
  "side_effects": [],
  "exceptions": [],
  "source_complete": true,
  "chunks_total": 4,
  "chunks_completed": 4,
  "status": "completed"
}
```

简单转发节点可以只有一句自身摘要，但它的子节点仍必须继续遍历和解释。

## 7. 节点聚合解释

### 7.1 聚合内容

节点聚合解释由以下内容构成：

1. 当前节点自身解释；
2. 当前节点到每个直接子节点的调用语境；
3. 每个直接子节点的聚合解释或结构化引用；
4. 当前节点对子节点返回值、异常和副作用的处理方式。

调用边语境至少包含：

```json
{
  "caller": "OrderService.createOrder",
  "callee": "StockService.reserve",
  "call_line": 87,
  "condition": "商品需要库存管理时",
  "result_usage": "预占失败则终止创建",
  "transaction_context": "订单事务内"
}
```

### 7.2 引用而非无限复制

存储层不把全部后代解释正文递归复制到每个祖先节点。聚合结果保存当前层摘要、结构化事实以及对子节点解释的引用：

```json
{
  "node_key": "mall::OrderService.createOrder",
  "summary": "完成订单校验、价格计算、库存预占和订单持久化",
  "business_flow": [],
  "business_rules": [],
  "state_changes": [],
  "side_effects": [],
  "exceptions": [],
  "children": [
    {
      "node_key": "mall::StockService.reserve",
      "relation": "预占订单商品库存"
    }
  ],
  "status": "completed"
}
```

人类阅读时沿引用渐进展开；机器使用时可按任务和 token 预算解析引用。入口节点的聚合解释直接作为 API 功能解释。

## 8. API 功能解释与覆盖校验

API 解释至少包含：

```json
{
  "method": "POST",
  "path": "/orders",
  "summary": "创建订单并完成库存预占",
  "main_flow": [],
  "alternative_flows": [],
  "business_rules": [],
  "state_changes": [],
  "side_effects": [],
  "exceptions": [],
  "external_dependencies": [],
  "entry_node_key": "mall::OrderController.create",
  "coverage": {
    "total_nodes": 18,
    "completed_nodes": 17,
    "partial_nodes": 1,
    "failed_nodes": 0,
    "unresolved_external_nodes": 2,
    "truncated": false
  }
}
```

发布前必须检查：

- 每个仓内可解析节点都有解释状态；
- 每个代码块都有明确行号范围；
- 数据库写入、外部调用、状态变化和异常出口已进入聚合结果；
- 调用图截断、动态调用和模型失败已显式记录；
- API 解释能够追溯到节点和源码证据。

校验失败可以产生 `partial` 快照，但不能伪装成完整结果。

## 9. 手动生成与快照生命周期

### 9.1 手动触发

只有用户执行“生成解释”操作时才允许调用模型。以下操作只更新结构数据或过期状态：

- 页面加载和刷新；
- `codegraph sync`；
- Git commit、checkout 或工作区文件变化；
- API 契约重新提取；
- 服务启动和定时维护。

### 9.2 固化生成输入

一次端点生成必须使用一致的源码与调用图。优先绑定 Git commit/tree hash；对未提交工作区，生成开始时记录所有相关文件内容摘要和调用图摘要。生成过程中检测到输入变化时，候选快照标记为 `outdated_during_generation`，不得无提示地发布为最新代码解释。

### 9.3 状态机

```text
pending → running → validating → completed
                  ├────────────→ partial
                  ├────────────→ failed
                  └────────────→ cancelled
```

每个 API 已发布快照与当前源码的关系另行表示：

```text
current   快照与当前源码、调用图一致
outdated  当前源码或调用图已经变化
missing   尚无可用快照
```

模型调用失败时保留上一份当前快照，不使用失败候选覆盖它。

### 9.4 原子发布

为某个 API 生成新快照 B 时，该 API 的当前快照 A 持续提供查询。B 完成校验并满足发布策略后，在一个事务内切换该端点的 `current_snapshot_id`。发布前的页面查询始终读取 A，发布后的新查询读取 B，不暴露混合版本。

### 9.5 删除

- 非当前快照可以直接删除。
- 删除当前快照需要显式确认。
- 删除当前快照后，该 API 直接进入 `missing` 状态，不自动回退旧快照。
- 旧快照只能在快照管理界面查看元数据和删除，不会被日常页面自动恢复为当前解释。
- `running` 快照必须先取消，再删除。
- 删除快照应级联删除其节点、代码块、调用边和 API 解释引用。

快照列表只承担查看生成状态、激活状态和删除管理，不提供节点级历史对比页面。

## 10. 手动生成中的增量复用

手动触发不等于每次全量调用模型。系统可以在用户触发后复用上一快照中仍然有效的解释。

### 10.1 摘要

```text
local_digest = hash(
    normalized_source
    + function_signature
    + model_id
    + local_prompt_version
    + output_schema_version
)

aggregate_digest = hash(
    local_explanation_digest
    + sorted(call_edges_with_context)
    + sorted(child_aggregate_digests)
    + aggregate_prompt_version
)
```

### 10.2 复用规则

| 变化 | 节点自身解释 | 节点聚合解释 |
|------|-------------|-------------|
| 当前函数源码或签名变化 | 重新生成 | 重新生成 |
| 当前函数调用边变化 | 视本地 Prompt 输入决定 | 重新生成 |
| 只有子节点实现变化 | 复用 | 重新生成 |
| 模型或 Prompt 版本变化 | 重新生成 | 重新生成 |
| 只有格式化变化且规范化哈希相同 | 复用 | 复用 |

复用只发生在人工生成任务内部，不构成自动解释刷新。

### 10.3 反向影响传播

变化节点的自身解释完成后，沿反向调用边标记所有祖先聚合解释待重建；随后按 SCC 压缩图的反向拓扑顺序，从变化叶子向 API 入口重新聚合。未受影响的子图直接复用。

为避免公共底层函数变化导致大量模型调用，结构化事实合并优先由确定性代码完成；只有需要重新撰写面向人的自然语言摘要时才调用模型。

## 11. 存储设计

建议新增以下 SQLite 表：

```sql
CREATE TABLE explanation_snapshots (
    id TEXT PRIMARY KEY,
    repo_name TEXT NOT NULL,
    member_name TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    handler TEXT NOT NULL,
    entry_node_key TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    graph_digest TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '{}',
    coverage TEXT NOT NULL DEFAULT '{}',
    statistics TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);

CREATE TABLE explanation_snapshot_nodes (
    snapshot_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    local_digest TEXT NOT NULL,
    aggregate_digest TEXT NOT NULL,
    local_explanation TEXT NOT NULL,
    aggregate_explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, node_key),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE explanation_snapshot_chunks (
    snapshot_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, node_key, chunk_index),
    FOREIGN KEY (snapshot_id, node_key)
        REFERENCES explanation_snapshot_nodes(snapshot_id, node_key) ON DELETE CASCADE
);

CREATE TABLE explanation_snapshot_edges (
    snapshot_id TEXT NOT NULL,
    caller_key TEXT NOT NULL,
    callee_key TEXT NOT NULL,
    call_site TEXT NOT NULL DEFAULT '{}',
    call_context TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, caller_key, callee_key, call_site),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id) ON DELETE CASCADE
);

CREATE TABLE current_explanation_snapshots (
    repo_name TEXT NOT NULL,
    member_name TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    PRIMARY KEY (repo_name, member_name, api_key),
    FOREIGN KEY (snapshot_id) REFERENCES explanation_snapshots(id)
);
```

所有 JSON 字段写入前应通过领域 DTO 或 schema 验证。快照删除与当前指针切换必须位于同一事务。

## 12. 应用服务边界

建议按现有分层新增：

```text
analysis/knowledge/
  explanation_graph.py       # 调用图、SCC 和反向拓扑
  source_chunking.py          # 函数语义分块
  explanation_validation.py  # 覆盖校验

application/
  explanation_generation_service.py  # 手动生成编排
  explanation_query_service.py       # 当前快照和节点查询
  explanation_snapshot_service.py    # 列表、取消、删除和当前指针

infrastructure/
  explanation_snapshot_store.py      # SQLite adapter

semantic/
  explanation_service.py      # 块、节点和聚合 Prompt 调用
```

分析层负责纯图算法、分块和验证；应用层负责生成生命周期与事务编排；基础设施层负责持久化；语义层负责模型调用和结构化解析；API 层只做请求校验和结果交付。

## 13. HTTP 接口建议

```text
POST   /api/api-explanations/generate
POST   /api/api-explanations/generate-batch
GET    /api/api-explanations/current?repo=<repo>&api_key=<api_key>
GET    /api/api-explanations/snapshots?repo=<repo>&api_key=<api_key>
GET    /api/api-explanations/snapshots/{snapshot_id}
DELETE /api/api-explanations/snapshots/{snapshot_id}
POST   /api/api-explanations/snapshots/{snapshot_id}/cancel

GET    /api/api-explanations/snapshots/{snapshot_id}/nodes/{node_key}
GET    /api/api-explanations/snapshots/{snapshot_id}/nodes/{node_key}/chunks
```

`generate` 请求体必须明确指定仓库成员、HTTP method、path 和 handler，并快速返回任务及候选快照 ID。`generate-batch` 也必须由用户明确操作触发，服务端不得自行创建批量任务。前端轮询状态或通过后续事件通道获取进度。任何 GET 接口都不能隐式触发模型。

## 14. 页面交互

### 14.1 API 契约页

- 展开端点后，左侧显示冻结快照中的后端调用链，右侧节点栏提供“生成 API 功能解释”和快照管理入口；页面下方仍保留状态摘要，便于查看覆盖率和快照元数据。
- 当前快照过期时显示源码版本和“手动刷新解释”。
- 默认展示 API 摘要、主流程和覆盖状态。
- 选中端点根、函数节点或跨服务节点后，右侧按 CodeGraph `node_id` 绑定对应快照节点，展示节点自身翻译和节点聚合结果；端点根的聚合结果就是 API 功能解释。
- 生成任务按后端的叶子优先顺序完成“节点自身翻译 → 父节点聚合”，页面轮询当前快照，生成期间继续展示旧快照。
- `partial`、`failed`、未解析和截断节点必须在树上可定位。

### 14.2 快照管理

- 按 API 展示快照 ID、源码版本、模型、状态、覆盖率和生成时间。
- 支持取消正在生成的快照。
- 支持删除已结束快照。
- 删除当前快照前明确告知该 API 将进入无快照状态。
- 不提供节点解释历史对比。

## 15. 并发、恢复与安全

- 同一个 API 同一时间只允许一个生成任务，避免竞争发布当前指针；仓库级并发由全局模型调用上限控制。
- 模型调用采用有限并发；相互独立的叶子节点可以并行，同一依赖链必须遵守自底向上顺序。
- 每个块和节点完成后持久化状态，使中断后的候选快照可继续或安全取消。
- Prompt 输入严格限制为已注册仓库内允许读取的源码和结构数据。
- 日志不得记录 API Key 和完整敏感源码；错误字段只保存必要诊断信息。
- 删除操作记录审计信息，但审计日志不保存已删除解释正文。

## 16. 验收标准

1. 简单入口调用复杂 Service 时，系统仍能遍历并解释 Service 及其后代。
2. 每个可解析调用链节点都能查看节点自身解释、聚合解释和源码位置。
3. 超过单次输入预算的函数被拆为多个有连续行号证据的代码块，没有静默截断。
4. 递归和循环调用不会造成无限任务，并产生 SCC 级解释。
5. 同一共享节点只生成一次自身解释，可被多个 API 引用。
6. 页面加载、代码修改和 CodeGraph 同步均不会调用模型。
7. 用户手动生成期间该 API 的旧快照持续可查询；新快照发布时原子切换。
8. 未变化节点可以跨快照复用，变化节点的聚合影响能够传播到所有相关 API。
9. 失败生成不会覆盖当前成功快照。
10. 任意快照均可按规则删除，删除当前快照后该 API 进入 `missing`，不会自动回退旧解释。

## 17. 分阶段实施

### 阶段一：快照骨架

- 建立快照表、当前指针、手动生成任务和删除接口。
- 将现有单节点解释写入快照作用域。
- 页面查询只读取当前快照。

### 阶段二：全图叶子优先解释

- 构建 API 可达调用图。
- 实现 SCC 压缩和反向拓扑调度。
- 自动生成全部节点自身解释和聚合解释。
- 增加节点覆盖状态。

### 阶段三：超大函数分块

- 实现语义分块和块级持久化。
- 增加行号证据、完整性校验和失败重试。
- 移除静默源码截断。

### 阶段四：增量复用与体验完善

- 引入本地摘要和聚合摘要。
- 实现反向影响传播和跨快照复用。
- 完成快照管理、过期提示、取消和覆盖率界面。
