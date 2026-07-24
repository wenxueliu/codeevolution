# CodeHistory — 代码仓功能演进分析系统设计文档

## 1. 项目定位

**CodeHistory** 通过分析历史 git 提交记录，逐步建立代码仓的功能演进过程。以"功能"（而非"符号"）为追踪单位，服务开发、测试、架构三类角色，最终形态为 MCP tools + CLI + Web Dashboard 的完整平台。

### 1.1 与现有工具的差异

| 工具 | 做什么 | 缺什么 |
|------|--------|--------|
| `codegraph` (npm) | 对**当前**代码建知识图谱（符号→边→SQLite） | 没有时间维度，不分析历史 |
| `code-review-graph` (PyPI) | 对 **PR diff** 做影响分析+风险评估 | 只看一个 PR，不跟踪长期演进 |
| **CodeHistory** | 沿 git 历史遍历，以每个 commit 为最小粒度建增量快照，跨快照追踪功能演变 | — |

## 2. 核心定义

```
功能 = 入口点 + 下游调用树

入口点 = API endpoint / CLI command / event handler / cron job
调用树 = 从入口点出发，沿 CALLS 边 BFS 可达的所有符号
演变   = 同一入口点的调用树在时间轴上的结构变化
```

### 2.1 为什么以"功能"为追踪单位

- **符号级追踪**的最大瓶颈是 Identity Matcher——函数重命名、拆分、合并场景下准确率无法保证
- **入口点变更频率**远低于内部函数，框架装饰器（`@app.get()`、`@RequestMapping`、`gin.POST()`）提供了稳定的识别锚点
- **功能级匹配**比符号级更可操作：同一 endpoint path + HTTP method 在 95% 以上的情况下就是同一个功能

### 2.2 功能匹配策略

跨 commit 识别"同一个功能"：

```
L1 (高置信度, >0.9): 入口点签名精确匹配
  - HTTP: endpoint path + HTTP method
  - CLI: command name + subcommand chain
  - Event: topic/queue name + handler pattern

L2 (中置信度, 0.6-0.9): 调用树结构相似度
  - 对 L1 未匹配的"消失功能"和"新增功能"计算图编辑距离
  - graph edit distance / max(call_tree_a, call_tree_b) > 0.7 → 候选匹配，标记为人审

L3 (低置信度, <0.6): 内容指纹匹配
  - 入口点代码位置的 AST 指纹相似度
  - 低于 0.6 → 丢弃，视为功能的新增/消亡
```

## 3. 多角色视角

同一份底层数据，三个消费视角：

```
                    ┌─────────────────────┐
                    │   Feature Registry   │
                    │   (入口点 + 调用树)   │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
    ┌──────────┐        ┌──────────┐        ┌──────────┐
    │ 开发视角  │        │ 测试视角  │        │ 架构视角  │
    │          │        │          │        │          │
    │ 影响分析  │        │ 回归范围  │        │ 腐化检测  │
    │ 代码考古  │        │ 覆盖盲区  │        │ 技术债   │
    │ 入职导航  │        │ 变更溯源  │        │ 分层破坏  │
    └──────────┘        └──────────┘        └──────────┘
```

### 3.1 开发视角

| 能力 | 描述 |
|------|------|
| 影响分析 | 改这个函数会影响哪些功能？回归测试范围是什么？ |
| 代码考古 | 这个函数为什么长这样？当初是谁在什么动机下写的？ |
| 入职导航 | 新人通过功能维度快速理解"认证是怎么做的"，而不是看代码目录 |

### 3.2 测试视角

| 能力 | 描述 |
|------|------|
| 回归范围 | 这次改动命中了哪些功能，需要跑哪些测试？ |
| 覆盖盲区 | 哪个功能最近频繁变动但测试覆盖没跟上？ |
| 变更溯源 | 这个测试的期望值是在哪次 commit 里被修改的？为什么？ |

### 3.3 架构视角

| 能力 | 描述 |
|------|------|
| 腐化检测 | 模块间出现了不该有的依赖？分层被打破了？ |
| 技术债累积 | 功能的实现从简单到复杂，演进路径是否符合最初设计？ |
| 分层破坏 | controller 直接调 DB 跳过了 service 层？ |

架构规则采用**自动学习 + 人工审核**策略：
1. 系统从历史数据中学习"正常模式"（如 controller 层的函数总是通过 service 层调用 model 层）
2. 偏离正常模式的变化标记为"候选违规"
3. 人工审核后，确认的规则锁定为强制执行

## 4. 架构设计

### 4.1 核心流程

```
git log --first-parent (按时间正序，每次一个 commit)
       │
       ▼
┌─────────────────────────────────────────────────┐
│  1. History Walker                               │
│  对每个 commit 做增量解析:                         │
│  - 第一个 commit: 全量 tree-sitter 解析            │
│  - 后续 commit: 仅解析变更文件 (git diff-tree)      │
│  - file_hash 去重: 未变文件复用已有解析结果          │
│  首次支持: Python, Java                           │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  2. Entry Point Detector                         │
│  仅扫描变更文件中的入口点装饰器/签名:               │
│  - 新入口点 → 新的候选功能                          │
│  - 已有入口点变更 → 更新调用树                       │
│  - 入口点消失 → 功能标记为 removed                   │
│  Python: @app.get, @app.post (Flask/FastAPI),    │
│          @router.get (FastAPI),                  │
│          __main__ + argparse/click               │
│  Java:   @GetMapping, @PostMapping (Spring),     │
│          @Path, @GET/@POST (JAX-RS),            │
│          main() 方法                             │
│  产出: Feature {entry_point, commit, call_tree}   │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  3. Feature Matcher                              │
│  将新入口点与已有功能列表匹配:                     │
│  L1: 入口点签名精确匹配 (>0.9) → 同一功能           │
│  L2: 调用树图编辑距离 (0.6-0.9, 人审) → 候选匹配   │
│  L3: AST 指纹匹配 (<0.6, 丢弃) → 视作新功能        │
│  产出: FeatureIdentity {feature_id, timeline}     │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  4. Evolution Analyzer                           │
│  仅对有变化的功能生成演变事件:                     │
│  - 调用树扩张/收缩 (节点数变化)                     │
│  - 新增/移除依赖                                   │
│  - 循环依赖出现/消失                               │
│  - 跨层调用检测                                    │
│  - 测试覆盖变化                                    │
└──────────────────────┬──────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│  5. Multi-Role Query Layer                       │
│  开发: impact_radius(feature, changed_symbols)    │
│        feature_timeline(feature_id)               │
│        feature_onboarding(feature_name)           │
│        blame_history(symbol)  ← commit级细粒度    │
│  测试: regression_scope(changed_symbols)           │
│        test_gap(feature_id)                       │
│        test_origin(test_case)                     │
│  架构: layer_violations(commit_hash)               │
│        tech_debt_hotspot()                        │
│        churn_vs_complexity()                      │
└─────────────────────────────────────────────────┘
```

### 4.2 数据模型

```sql
-- git 提交记录
CREATE TABLE commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    parent_hash TEXT,
    timestamp INTEGER NOT NULL,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    semantic_type TEXT  -- feat/fix/refactor/docs 等 conventional commit 类型
);

-- 功能定义（跨 commit 稳定标识）
CREATE TABLE features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stable_id TEXT UNIQUE NOT NULL,       -- 全局唯一的功能 ID
    canonical_name TEXT NOT NULL,         -- 最新的入口点名称
    entry_type TEXT NOT NULL,             -- http/cli/event/cron
    entry_signature TEXT NOT NULL,        -- HTTP: "POST /api/login", CLI: "main", etc.
    first_seen_at INTEGER NOT NULL REFERENCES commits(id),
    last_seen_at INTEGER REFERENCES commits(id),
    status TEXT DEFAULT 'active'          -- active/deprecated/removed
);

-- 每个 commit 中功能的状态（仅存储有变化的 commit）
CREATE TABLE feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id),
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    call_tree_nodes INTEGER NOT NULL,     -- 调用树中的节点数
    call_tree_edges INTEGER NOT NULL,     -- 调用树中的边数
    call_tree_depth INTEGER NOT NULL,     -- 最大调用深度
    cyclomatic_complexity REAL,           -- 该功能的圈复杂度
    file_path TEXT NOT NULL,              -- 入口点文件路径
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    entry_point_node_id TEXT,             -- 对应 tree-sitter 节点 ID
    test_nodes INTEGER DEFAULT 0,         -- 关联的测试节点数
    UNIQUE(feature_id, commit_id)
);

-- 演变事件（仅当功能实际变化时产生）
CREATE TABLE evolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id),
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    event_type TEXT NOT NULL,             -- BORN/MODIFIED/GROWN/SHRUNK/EXTENDED/CONTRACTED/
                                          -- DEP_CREATED/DEP_REMOVED/CYCLE_ADDED/CYCLE_REMOVED/
                                          -- LAYER_VIOLATION_ADDED/LAYER_VIOLATION_FIXED/DIED
    detail JSON,                          -- 事件详情（变化前后的对比数据）
    UNIQUE(feature_id, commit_id, event_type)
);

-- 架构规则（自动学习 + 人工锁定）
CREATE TABLE architecture_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,              -- layer_constraint/forbidden_dependency/coupling_threshold
    source_pattern TEXT NOT NULL,
    target_pattern TEXT NOT NULL,
    is_violation BOOLEAN NOT NULL,        -- TRUE = 禁止，FALSE = 允许
    confidence REAL DEFAULT 1.0,          -- 1.0 = 人工锁定, <1.0 = 自动学习
    discovered_from TEXT,                 -- 从哪个 commit 学习到的
    locked_by TEXT,                       -- 谁锁定的（NULL = 未锁定）
    created_at INTEGER NOT NULL
);
```

### 4.3 存储策略

- **Delta-storage**：只存变化。feature_snapshots 和 evolution_events 仅在功能实际变化时写入，不是每个 commit 都产生记录
- 底层 tree-sitter 解析结果（nodes/edges）复用 codegraph 的 schema，通过 `commit_id` 区分版本
- **写时去重**：同一文件内容不变（file_hash 相同），不会重新解析，已有的解析结果通过引用复用
- 查询功能在某 commit 的状态时，回溯到最近的 snapshot 记录，无需 apply 中间的 diff chain

## 5. 增量更新机制

增量是核心能力。粒度为**每个 commit**，不依赖 tag。

### 5.1 分析粒度

```
粒度 = 每次 commit（git log --first-parent）

Commit E ──→ Commit F ──→ Commit G ──→ Commit H (HEAD)
   │            │            │            │
   ▼            ▼            ▼            ▼
 git diff     git diff     git diff     git diff
 E 变更文件    F 变更文件    G 变更文件    H 变更文件
   │            │            │            │
   ▼            ▼            ▼            ▼
 增量解析     增量解析     增量解析     增量解析
```

**为什么是每次 commit？**
- 不依赖 tag（很多仓库没有 tag）
- 最自然的变更单位，每个 commit 是一个逻辑变更
- 增量解析下，一般 commit 只改 1-3 个文件，解析成本极低
- 提供最细粒度的演变追踪

### 5.2 初始化模式：全量回填

```
首次运行，按时间正序遍历 git log --first-parent:
  commit_1 ──→ commit_2 ──→ commit_3 ──→ ... ──→ HEAD
     │             │             │                    │
     ▼             ▼             ▼                    ▼
  全量解析     增量解析      增量解析             增量解析
  (整个仓库)   (仅 diff 变更)  (仅 diff 变更)     (仅 diff 变更)
```

- **commit_1** (最早的提交): 完整 checkout + tree-sitter 全量解析
- **后续每个 commit**: 只解析 diff 中的变更文件，复用前一 commit 的未变更部分
- `file_hash` 去重确保同内容文件只在 DB 中存一份解析结果

### 5.3 增量模式：持续追踪

初始化完成后，新 commit push 到仓库时：

```
git log <last_analyzed_commit>..HEAD --first-parent
       │
       ▼
对每个新 commit:
  1. git diff-tree --name-only <parent> <commit>
  2. 只解析变更文件
  3. 扫描变更文件中的入口点
  4. 与已有功能列表匹配
  5. 仅对变化的功能生成演变事件
  6. 更新功能列表 + 事件表
```

### 5.4 演进事件的产生条件

**不是每个 commit 都产生事件**。只有当入口点或其调用树**实际变化**时才生成 snapshot + event：

```
入口点变更? ──→ Yes → 重新提取调用树 → 对比旧调用树 → 生成事件
入口点没变? ──→ 但调用树中的某个节点变更了? → Yes → 生成事件
               调用树也没变? → No → 跳过此 commit (不产生 snapshot/event)
```

大多数 commit 对大多数功能都是 No，因此实际存储的事件量远小于 commit 数量。

### 5.5 同名 commit 语义聚类（可选优化）

对于 conventional commits 格式的仓库，可以选择将连续的、同一作者、同一类型（都是 `fix:` 或都是 `refactor:`）的 commit 合并为一个逻辑分析单元，减少细碎事件：

```
commit_1 (fix: typo) ┐
commit_2 (fix: typo2) ├─→ 合并为一个逻辑变更单元
commit_3 (fix: typo3) ┘
commit_4 (feat: new api) ──→ 独立单元
```

此功能默认关闭，通过配置 `cluster_window_minutes=60` 开启。

### 5.6 与 tag 的关系

虽然不以 tag 为分析粒度，但 tag 信息会被记录到 commits 表中作为时间轴上的标记点，查询时可以用 tag 作为便捷的时间锚点：

```sql
-- commits 表可选扩展字段
ALTER TABLE commits ADD COLUMN tags TEXT;  -- JSON array: ["v1.0.0", "release-2024-q1"]
```

查询时 `feature_timeline(feature_id, from="v1.0.0", to="v2.0.0")` 通过 tag 解析为 commit hash 范围。

## 6. 技术选型

| 层级 | 技术 | 理由 |
|------|------|------|
| 解析引擎 | tree-sitter (复用 codegraph 管线) | 40+ 语言支持，生产验证 |
| 图存储 | SQLite (复用 codegraph schema) | 轻量、零外部依赖、WAL 模式支持并发读 |
| 图遍历 | BFS/DFS (复用 codegraph GraphTraverser) | 影响分析、调用树提取 |
| diff 解析 | 复用 code-review-graph `changes.py` | 已实现 `parse_git_diff_ranges()` |
| 社区检测 | Leiden 算法 (复用 code-review-graph 思路) | 跟踪模块/社区随时间的演变 |
| MCP 接口 | FastMCP (Python) 或 MCP SDK (TypeScript) | 待定，取决于主语言选型 |
| 语言 | Python 3.10+ (第一版双语言: Python + Java) | 与参考项目一致，AST 管线成熟 |

### 6.1 语言选型决策

**推荐 Python**。理由：
- code-review-graph 是 Python，codegraph 是 TypeScript
- 系统的核心逻辑在 Walker/Matcher/Analyzer 层（非解析层），这些是业务逻辑而非性能瓶颈
- Python 生态的 git 操作库（pygit2/libgit2）更成熟
- 解析层本身通过 tree-sitter 的 language pack 处理，Python 有 `tree-sitter-language-pack`
- 后续 NotebookLM/报告导出等集成也是 Python 生态更丰富

## 7. 交付路线图

```
Phase 1 (1-2 周)    MCP tools 可用
  └─ feature_timeline + impact_radius + regression_scope

Phase 2 (3-4 周)    CLI 命令
  └─ evolution trace / diffoscope / hotspot / coupling

Phase 3 (5-8 周)    Web Dashboard
  └─ 时间线可视化 + 依赖图 + 腐化热力图

Phase 4 (后续)       报告导出 + 跨仓库分析
```

### 7.1 Phase 1 验收标准

- 在外部目标仓库上运行，获得功能的完整演进时间线
- 支持 Python (Flask/FastAPI/Django) 和 Java (Spring Boot/JAX-RS) 的入口点识别
- MCP tool: `get_feature_timeline(feature_name)` 返回该功能从诞生到当前的所有演变事件
- MCP tool: `get_impact_radius(changed_symbols)` 返回某个改动影响的功能范围
- MCP tool: `get_regression_scope(changed_symbols)` 返回需要回归测试的功能列表

### 7.2 Phase 1 不做什么

- 不处理模糊匹配（L2/L3 Feature Matcher），只做 L1 精确匹配
- 不做自动学习架构规则，只接受手工规则 YAML 配置
- 不做 Web Dashboard 和 CLI 命令
- 增量模式不做文件变更的依赖传播（只解析直接变更文件，不做受影响文件的级联重解析）
- 不处理 merge commit 的非线性历史（只看 first-parent）

## 8. 关键设计决策

### 8.1 Merge Commit 处理

只看 **first-parent**。代码演进分析关心的是"合入主线的时刻"，而不是"在分支上怎么迭代的"。

### 8.2 Commit 语义聚类

利用 git log 携带的结构化信息，将连续的、同一作者、同一类型（conventional commits 前缀）的 commit 合并为一个逻辑变更单元，减少 Event Store 的噪声。

### 8.3 性能策略

- **以 commit 为粒度，但只解析变更文件**：每个 commit 一般只改 1-3 个文件，`git diff-tree --name-only` + tree-sitter 增量解析成本极低
- **大部分 commit 不产生事件**：只有入口点或其调用树实际变化时才写 snapshot/event 记录
- **file_hash 去重**：不变的文件复用已经解析的结果引用，避免重复 tree-sitter 计算
- **依赖传播（Phase 2）**：变更文件的 import 方也重解析（2 跳 BFS），确保调用边不遗漏
- `git ls-files` 作为文件列表快速获取方式（codegraph 已验证）

## 9. 参考项目

| 项目 | 位置 | 复用内容 |
|------|------|----------|
| codegraph | `reference/codegraph/` | tree-sitter 解析管线、SQLite schema、BFS/DFS 遍历、MCP 工具架构 |
| code-review-graph | `reference/code-review-graph/` | git diff 解析、风险评分模型、社区检测(Leiden)、增量更新策略 |
