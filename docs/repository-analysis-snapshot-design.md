# 仓库级解析快照设计

> 状态：已确认，待实施
>
> 日期：2026-09-09
>
> 范围：手动解析、仓库级快照、最新快照组合视图、失败重试、历史快照、旧 Evolution Engine 移除

## 1. 背景

当前 Web 知识中心在每次请求 `/api/knowledge` 时直接打开注册仓库的
`.codegraph/codegraph.db` 并重新执行知识提取。调用树、节点规则、仓库问答、API
解释和跨仓拓扑也会读取当前 CodeGraph 或当前工作区源码。

这种行为存在以下问题：

- 打开页面等同于重新分析，用户无法决定解析时机；
- 主报告、调用树和节点解释可能来自不同时间点；
- 仓库变化后无法稳定查看旧结果；
- 多仓中某个仓库失败时，缺少仓库维度的重试和状态；
- 当前拓扑缓存只按 CodeGraph DB 的修改时间判断新鲜度，无法表达输入组合；
- 旧 Evolution Engine 以 Git commit 为时间轴，与本设计的用户触发时间点语义不同。

本设计将 CodeEvolution 定位为个人/本地分析工作台：用户选择仓库并手动生成当下
时间点的解析快照。CodeEvolution 不负责管理团队发布版本，也不负责判断多个仓库的
哪些提交彼此兼容。团队可以在外部流程中为快照或固定组合赋予自己的版本语义。

## 2. 设计目标

1. 只有用户显式触发时才读取并解析现场仓库。
2. 用户可以选择任意已注册物理仓库，不要求一次解析全部成员。
3. 每个物理仓库独立生成、发布、失败和重试。
4. 仓库允许包含未提交代码；Git 信息仅作为采集证据。
5. 所有知识读取绑定不可变快照，不再回退到现场 CodeGraph 或源码。
6. 代码图谱组合每个仓库的最新成功快照。
7. 页面浏览期间固定组合版本，避免一次浏览中发生版本漂移。
8. 保存足够的 CodeGraph 和源码证据，使旧快照仍可查询调用树并生成解释。
9. 新解析失败不影响该仓库上一次成功快照。
10. 支持持久任务状态、仓库维度重试、取消和服务重启后的失败恢复。
11. 移除旧 Git Evolution Engine，未来的演进能力改为比较用户生成的解析快照。

## 3. 非目标

- 不自动遍历 Git 历史；
- 不管理 branch、tag、release、部署环境或团队版本；
- 不为多仓选择兼容 commit；
- 不自动 checkout、merge、pull 或修改用户代码；
- 不在页面加载、状态刷新或后台定时任务中自动解析；
- 不在基础解析中自动执行 LLM 调用；
- 不把 Git commit 作为解析快照身份；
- 不删除升级前已经存在于磁盘的 `evolution.db`。

## 4. 领域术语

### 4.1 Analysis Scope（分析范围）

注册的逻辑分组，包含一个或多个物理仓库。它只表达仓库组织关系，不表达版本或发布。

### 4.2 Repository Member（仓库成员）

分析范围内的一个物理 Git 仓库。仓库成员使用永久 `member_id` 标识，名称和本地路径
只是可修改属性。

### 4.3 Analysis Run（解析运行）

一次用户操作，目标是任意一组 `member_id`。同一 Run 中各仓库独立执行，不形成全成
全败事务。

### 4.4 Repository Attempt（仓库尝试）

Run 中某个仓库的一次执行。失败重试创建新的 Attempt，保留之前的时间、输入摘要、
阶段和错误。

### 4.5 Repository Analysis Snapshot（仓库解析快照）

一个仓库在用户选择的时间点生成的不可变解析结果，包括 CodeGraph、必要源码证据和
单仓派生知识。

### 4.6 Current Repository Snapshot（仓库当前快照）

仓库最新成功的解析快照。最近一次 Attempt 失败、取消或中断时，current 仍指向上一次
成功结果。

### 4.7 Graph View（图谱视图）

一次稳定浏览使用的 `member_id -> snapshot_id` 映射。普通页面加载时根据所有仓库
current 指针生成并固定 View；用户刷新后才切换到更新后的组合。

### 4.8 Pinned Graph View（固定图谱视图）

被用户固定、分享、导出，或被解释与规则引用的持久 Graph View。

### 4.9 Capture Time（采集时间）

用户理解和浏览历史使用的时间。时间不是内容身份；内部使用 snapshot ID 和内容摘要。

### 4.10 Observed Git State（观察到的 Git 状态）

采集时的 branch、HEAD commit 和 dirty 状态，仅用于来源说明，不限制快照创建，也不
参与仓库版本管理。

## 5. 核心决策

### 5.1 仓库独立发布

用户选择 A、B、C 三个仓库时：

| 仓库 | 本次结果 | current 行为 |
|---|---|---|
| A | 成功 | 指向新快照 |
| B | 失败 | 保留旧快照并显示最近失败 |
| C | 内容未变化 | 保持原快照，Attempt 标记 `unchanged` |

Run 可以是 `completed`、`partial` 或 `failed`，但仓库成功结果不因其他仓库失败而回滚。

### 5.2 “最新”的含义

“最新版本”统一指仓库的最新成功解析快照，不表示现场工作区的最新内容，也不表示
最新 Git commit。

UI 同时展示：

- `current_snapshot`：实际用于查询的最新成功结果；
- `last_attempt`：最近解析是否失败、取消或中断；
- `source_changed`：用户显式检查后发现现场输入发生变化；
- `analyzer_outdated`：源码未变，但 CodeEvolution、CodeGraph、规则或 schema 已升级。

### 5.3 页面固定组合

用户打开知识或图谱页面时，服务解析 current 指针并生成：

```text
view_digest = SHA256(canonical(member_id + snapshot_id ...))
```

后续调用树、节点规则和跨仓查询都携带该 View。即使后台有仓库产生新快照，当前页面
仍使用原组合；用户点击“刷新最新快照”后才取得新的 View。

### 5.4 单仓事实与组合事实分离

`RepositoryAnalysisSnapshot` 只保存单仓事实，包括 API、模块、实体、测试缺口、分层、
配置、依赖、权限和热力图。

依赖多个仓库的结果保存为 `GraphViewArtifact`，包括：

- 跨服务 Topology；
- HTTP/MQ/gRPC/Redis Flow；
- 跨服务实体对齐；
- 前端调用者与后端 API 的关联；
- 基于组合的 impact 结果。

组合产物按 `view_digest + analyzer/rule version` 缓存。任一 current 指针改变只会产生
新的组合摘要，不修改旧仓库快照或旧组合产物。

## 6. 数据模型

建议使用独立的全局数据库：

```text
${CODEEVOLUTION_DATA_DIR:-~/.codeevolution}/analysis-snapshots.db
```

不得复用旧 `evolution.db`。

### 6.1 仓库身份

```sql
repository_members (
    id                 TEXT PRIMARY KEY,
    scope_name         TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    registered_path    TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    retired_at         INTEGER
);
```

现有 registry 首次迁移时为每个成员补充永久 UUID。历史快照通过 `member_id` 关联，不
依赖目录 basename 或本地路径。

### 6.2 Run 与 Attempt

```sql
analysis_runs (
    id                 TEXT PRIMARY KEY,
    status             TEXT NOT NULL,
    requested_at       INTEGER NOT NULL,
    completed_at       INTEGER,
    external_context   TEXT NOT NULL DEFAULT '{}'
);

repository_attempts (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    member_id          TEXT NOT NULL,
    attempt_no         INTEGER NOT NULL,
    status             TEXT NOT NULL,
    stage              TEXT NOT NULL,
    requested_at       INTEGER NOT NULL,
    started_at         INTEGER,
    completed_at       INTEGER,
    source_digest      TEXT,
    snapshot_id        TEXT,
    retry_of           TEXT,
    error_code         TEXT,
    error_message      TEXT,
    log_excerpt        TEXT,
    UNIQUE(run_id, member_id, attempt_no)
);
```

Attempt 状态：

```text
pending -> initializing/syncing -> capturing -> analyzing -> saving
        -> completed | unchanged | failed | cancelled | interrupted | already_running
```

同一物理仓库最多存在一个活动 Attempt。约束必须持久化，不能只用进程内锁。

### 6.3 仓库快照

```sql
repository_analysis_snapshots (
    id                    TEXT PRIMARY KEY,
    member_id             TEXT NOT NULL,
    captured_at           INTEGER NOT NULL,
    source_digest         TEXT NOT NULL,
    graph_digest          TEXT NOT NULL,
    observed_branch       TEXT,
    observed_head_commit  TEXT,
    dirty                 INTEGER NOT NULL,
    codegraph_version     TEXT NOT NULL,
    analyzer_version      TEXT NOT NULL,
    schema_version        TEXT NOT NULL,
    rules_version         TEXT NOT NULL,
    options_digest        TEXT NOT NULL,
    report_json           TEXT NOT NULL,
    artifact_key          TEXT NOT NULL,
    source_completeness   TEXT NOT NULL,
    label                 TEXT NOT NULL DEFAULT '',
    note                  TEXT NOT NULL DEFAULT '',
    pinned                INTEGER NOT NULL DEFAULT 0,
    orphaned              INTEGER NOT NULL DEFAULT 0,
    UNIQUE(
        member_id,
        source_digest,
        graph_digest,
        analyzer_version,
        schema_version,
        rules_version,
        options_digest
    )
);

current_repository_snapshots (
    member_id             TEXT PRIMARY KEY,
    snapshot_id           TEXT NOT NULL
);
```

快照内容和采集证据不可修改；只允许更新独立用户元数据 `label/note/pinned`。

### 6.4 图谱视图

```sql
graph_views (
    id                  TEXT PRIMARY KEY,
    digest              TEXT UNIQUE NOT NULL,
    created_at          INTEGER NOT NULL,
    pinned              INTEGER NOT NULL DEFAULT 0,
    label               TEXT NOT NULL DEFAULT '',
    note                TEXT NOT NULL DEFAULT '',
    external_context    TEXT NOT NULL DEFAULT '{}'
);

graph_view_members (
    view_id             TEXT NOT NULL,
    member_id           TEXT NOT NULL,
    snapshot_id         TEXT,
    availability        TEXT NOT NULL,
    PRIMARY KEY(view_id, member_id)
);

graph_view_artifacts (
    view_digest         TEXT NOT NULL,
    artifact_type       TEXT NOT NULL,
    analyzer_version    TEXT NOT NULL,
    rules_version       TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    PRIMARY KEY(view_digest, artifact_type, analyzer_version, rules_version)
);
```

普通 current View 可只计算 digest 而不持久化；当 View 被固定、分享、导出或引用时才
写入 `graph_views` 和 `graph_view_members`。

没有成功快照的注册成员仍进入 View，`availability=unparsed`，View 标记 `incomplete`。
系统不得静默忽略，也不得回退读取现场仓库。

## 7. Artifact 布局

大型不可变产物保存在数据目录，不写入被分析仓库：

```text
~/.codeevolution/
  analysis-snapshots.db
  artifacts/
    sha256/<prefix>/<digest>/
      codegraph.db
      sources/
      manifest.json
```

要求：

- 使用内容寻址复用相同产物；
- CodeGraph DB 使用 SQLite backup API 生成一致性副本，不能裸复制 WAL 数据库；
- 完成 integrity check、摘要计算和 fsync 后，才在数据库事务中发布 current；
- 临时文件使用同文件系统临时目录并原子 rename；
- artifact 目录权限仅当前用户可读写；
- 快照数据库只记录 artifact key 和摘要，不把大型 SQLite 文件存成 BLOB。

## 8. 源码冻结范围与安全

保存内容：

1. CodeGraph `files` 表覆盖的源码文件；
2. 知识提取过程中实际读取的白名单配置文件；
3. 生成跨仓证据需要的 URL、topic、channel 等源码文件；
4. manifest 中记录每个保存文件的相对路径、大小和 SHA-256。

排除内容：

- `.git`；
- `.env` 及其变体；
- 私钥、证书和已知 secret 文件；
- 二进制和大型生成产物；
- Git ignored 文件；
- 指向仓库根目录外部的 symlink；
- 未显式注册的 submodule 内容。

若分析依赖被排除文件，快照仍可生成，但必须标记 `source_completeness=incomplete` 并列出
排除证据，不能假装完整。

Submodule 默认只记录路径和状态。需要完整分析时，应将其作为独立 Repository Member
注册。

## 9. 手动解析流程

### 9.1 请求

用户从任意逻辑服务中选择一个或多个 `member_id`。一次 Run 可以跨逻辑服务选择成员。

基础解析始终执行完整的确定性知识维度，不开放 API/模块/实体等维度选择，也不自动
执行 LLM。

### 9.2 单仓 Attempt

```text
1. 获取持久化 member 互斥锁
2. 校验仓库仍注册、路径存在且是 Git 仓库
3. 采集同步前输入摘要和 Git 辅助状态
4. 无 CodeGraph DB：执行 codegraph init
5. 已有 CodeGraph DB：执行 codegraph sync
6. 再次采集输入摘要
7. 摘要变化：failed(source_changed_during_capture)
8. 使用 SQLite backup 冻结 CodeGraph DB
9. 冻结必要源码证据
10. 运行现有完整非 LLM 单仓知识提取
11. 计算 source/graph/report/artifact digest
12. 输入与 current 完全一致：unchanged
13. 否则事务写入快照并切换该 member current
14. 释放锁并更新 Run 汇总状态
```

不得在现场仓库执行 checkout、reset、pull 或 commit。

### 9.3 有界并发

- 默认同时解析 2 个仓库；
- 并发度可配置；
- 同一 member 只能有一个活动 Attempt；
- 后来的请求遇到活动 member 时返回 `already_running` 和现有任务 ID；
- 同一请求内其他未冲突 member 继续执行。

### 9.4 取消

- 支持取消单个仓库；
- 支持取消 Run 中尚未结束的所有仓库；
- pending 任务直接标记 cancelled；
- 正在运行的 subprocess 先 SIGTERM，等待超时后结束；
- 已成功发布的其他仓库不回滚；
- cancelled Attempt 不改变 current。

### 9.5 重试

- 只重试用户选择的失败、取消或中断 member；
- 每次重试创建新的 Attempt；
- 重试读取当下工作区，不试图恢复第一次失败时的源码；
- `retry_of` 保留审计关系；
- 重试成功后独立发布新快照。

### 9.6 服务重启

任务状态必须存数据库。服务启动时将遗留的 pending/running Attempt 标记为 interrupted，
保留阶段和日志。系统不恢复旧 subprocess，由用户决定是否重试。

## 10. 改动检查

普通仓库列表、知识页和图谱页完全不访问现场仓库。

用户点击“检查改动”时才计算轻量输入摘要，覆盖：

- CodeGraph 可索引源码；
- 分析器白名单配置；
- 会参与分析的未提交和未跟踪文件；
- Git branch、HEAD 和 dirty 仅作为展示信息。

检查结果区分：

- `unchanged`；
- `source_changed`；
- `analyzer_outdated`；
- `unparsed`；
- `check_failed`。

检查不执行 `codegraph sync`，不创建快照，也不自动开始解析。

## 11. 读取模型

### 11.1 单仓知识

读取指定 `repository_snapshot_id` 的 `report_json`、冻结 CodeGraph DB 和冻结源码。

### 11.2 聚合知识

根据 Graph View 中的成员快照合并单仓报告。前端调用者关联等组合信息由对应
`GraphViewArtifact` 提供。

### 11.3 调用树

节点 ID 只在仓库快照内有效。请求必须携带 View 或 repository snapshot ID；服务从
artifact CodeGraph DB 查询，并通过 SnapshotSourceProvider 读取冻结源码。

### 11.4 Node Rule、Business Rule 与 API Explanation

- 新结果必须绑定 `repository_snapshot_id`；
- 已有未绑定结果保留为 `legacy_unbound`，可以显示但明确警告；
- 新旧结果不再原地覆盖；
- API 解释 freshness 比较绑定的 repository snapshot 与 View，而不是重新读取现场仓库；
- 被解释或规则引用的 repository snapshot 自动受到删除保护。

### 11.5 Repository Chat

删除 Evolution feature/event/stats 操作。符号搜索、调用者和知识问答只访问 current
snapshot 或页面固定 View。无快照时返回“尚未解析”。

### 11.6 MCP

保留 MCP，但删除 Git Evolution timeline/history/summary 工具，替换为：

- 列出仓库与 current snapshot；
- 获取仓库快照元数据和知识报告；
- 搜索快照符号；
- 查询调用者与调用树；
- 获取或查询固定 Graph View。

## 12. API 草案

### 12.1 仓库和状态

```http
GET /api/repos
GET /api/repos/{scope}/members
POST /api/repository-members/check
```

成员响应包含：

```json
{
  "member_id": "...",
  "name": "mall-api",
  "current_snapshot": {},
  "last_attempt": {},
  "change_status": "source_changed"
}
```

### 12.2 Run

```http
POST /api/analysis-runs
GET /api/analysis-runs/{run_id}
POST /api/analysis-runs/{run_id}/retry
POST /api/analysis-runs/{run_id}/cancel
POST /api/analysis-runs/{run_id}/members/{member_id}/cancel
```

创建请求：

```json
{
  "member_ids": ["member-a", "member-b"],
  "external_context": {}
}
```

### 12.3 快照

```http
GET /api/repository-members/{member_id}/snapshots
GET /api/repository-snapshots/{snapshot_id}
PATCH /api/repository-snapshots/{snapshot_id}/metadata
DELETE /api/repository-snapshots/{snapshot_id}
```

删除 current、pinned 或被 View、规则、解释引用的快照时返回 409 和引用清单。不提供
普通 UI 级联强删。

### 12.4 View

```http
POST /api/graph-views/current
POST /api/graph-views/{view_digest}/pin
GET /api/graph-views/{view_id}
GET /api/graph-views/{view_id}/export
```

固定/导出清单只表达 `member_id -> repository_snapshot_id`，不表达 Git release。

### 12.5 知识读取

```http
GET /api/knowledge?view_id=...
GET /api/call-tree/children?view_id=...&repository_snapshot_id=...
GET /api/call-tree/rule?view_id=...&repository_snapshot_id=...
POST /api/api-explanations/generate
```

所有读取必须可追踪到 View 和具体仓库快照。缺失时返回明确状态，不允许 live fallback。

## 13. 前端设计

### 13.1 仓库列表

移除“一键初始化并回溯历史”，替换为：

- 成员 checkbox；
- 全选；
- 解析已选择仓库；
- 检查改动；
- 重试失败仓库；
- current 快照时间、短 ID、branch/HEAD/dirty 辅助信息；
- last Attempt 状态和错误；
- 未解析、源码变化、分析器过期提示。

### 13.2 进度

按仓库展示：

```text
等待 -> 初始化/同步图谱 -> 冻结源码 -> 分析 -> 保存 -> 完成
```

部分失败时已成功仓库立即显示新 current；失败仓库显示旧 current 和重试按钮。

### 13.3 知识中心

- 页面创建时只加载 current View；
- 不自动解析；
- View 在页面生命周期内固定；
- 提供“刷新最新快照”；
- 无快照时展示仓库级未解析状态；
- 支持仓库快照历史、label、note、pin；
- 支持固定/导出当前 View。

## 14. 保留、删除与磁盘空间

默认策略：

- 每仓保留最近 10 个成功快照；
- current、pinned 和被引用快照永不自动清理；
- 失败 Attempt 只保存日志，默认保留 30 天；
- artifact 内容寻址，相同文件/图谱允许复用；
- 删除仓库注册不会删除历史快照；历史标记 orphaned/retired；
- 物理删除必须独立操作并二次确认。

解析前进行磁盘空间预检。无法满足安全余量时拒绝开始并列出可清理项；不得自动删除
current、pinned 或被引用产物。

## 15. 外部团队集成

CodeEvolution 始终生成时间点快照，不内建团队版本模型。

Run 和 Pinned Graph View 可保存大小受限的 `external_context` JSON 与 tags，例如外部
系统自行定义的 pipeline、release 或工单引用。CodeEvolution 原样保存和返回，不解释、
不校验，也不使其参与快照 digest、current 选择或解析逻辑。

团队需要固定知识状态时，固定并导出 Graph View：

```json
{
  "view_id": "view-...",
  "view_digest": "sha256:...",
  "members": {
    "member-a": "snapshot-a3",
    "member-b": "snapshot-b7"
  }
}
```

团队如何将其映射到 Git commit、发布版本或知识库，由外部团队流程决定。

## 16. 旧 Evolution Engine 移除

### 16.1 删除范围

删除整个 Git 历史演进子系统，而不只是隐藏命令：

- Git HistoryWalker；
- EvolutionEngine；
- FeatureMatcher；
- EvolutionAnalyzer；
- EvolutionCommandService / EvolutionQueryService；
- commit/features/feature_snapshots/evolution_events 读写代码；
- 未公开使用且绑定旧 feature FK 的 Runtime Telemetry；
- CLI `backfill`、`update`、演进 `status`；
- MCP timeline/history/evolution summary 工具；
- Chat 的 feature/event/stats 查询；
- Web `/api/repos/{name}/init` 旧流程；
- 对应测试、benchmark 和当前态文档描述。

保留：

- 基于当前图谱的 topology、impact、trace、flow、entities；
- CodeGraph `init/sync` 能力；
- CLI/Web 服务状态命令；
- 用户磁盘中已有的 `.codeevolution/evolution.db` 和旧 `.codehistory/evolution.db`。

### 16.2 兼容策略

这是明确的破坏性变更：不保留旧 CLI/API/MCP 兼容别名，发布说明中列出移除项。
`/api/repos/{name}/init` 不映射到新解析 API，因为旧接口是全成员和历史回溯语义，
映射后会造成误解。

### 16.3 新的 Evolution 含义

保留 CodeEvolution 产品名。未来“Evolution”表示两个用户生成的 Repository Analysis
Snapshot 之间的变化，而不是自动 Git 历史遍历。第一阶段不实现差异分析，但快照身份、
时间和不可变结果应支持后续比较。

## 17. 迁移策略

1. 新数据库独立创建，使用显式 `PRAGMA user_version` 迁移。
2. registry 为所有旧成员补充稳定 member UUID，写入使用原子 JSON replacement。
3. 已有 `.codegraph/codegraph.db` 不自动导入；成员初始显示“尚未解析”。
4. 用户首次选择解析时必须执行 `codegraph sync` 后才能生成首个快照。
5. 旧 Business Rule、Node Rule 和 API Explanation 标记 `legacy_unbound`。
6. 旧 topology cache 不作为正式 Graph View，首次访问新 View 时重新构建。
7. 旧 evolution.db 保留在磁盘，不迁移、不删除、不继续写入。
8. 历史规划和审计文档保留历史语境，可增加 superseded 提示，不重写历史事实。

## 18. 实施阶段

### Phase 1：仓库快照核心

- 稳定 member ID；
- Run/Attempt 持久化；
- 选择性仓库解析；
- init/sync、内容校验、CodeGraph backup 和源码冻结；
- 单仓报告和 current 指针；
- retry/cancel/interrupted/unchanged；
- 仓库列表和手动解析 UI。

### Phase 2：快照绑定读取

- Knowledge 从快照读取；
- SnapshotSourceProvider；
- Call Tree、Node Rule、Chat 改为 snapshot-bound；
- API Explanation 绑定仓库快照；
- legacy_unbound 展示。

### Phase 3：Graph View

- current View 和页面固定；
- 按 view digest 的跨仓 topology/flow/entities/frontend-callers；
- pinned view 和 JSON 导出；
- 引用保护和历史浏览。

### Phase 4：清理和演进替换

- 完整移除旧 Evolution Engine、Runtime Telemetry 和旧入口；
- 重做 MCP 快照知识工具；
- 更新 CLI、README、AGENTS 和设计文档；
- 增加仓库快照之间的未来 diff 扩展点，但暂不实现 diff。

## 19. 验收标准

### 19.1 手动与隔离

- 打开任何普通页面不会运行 CodeGraph 或扫描现场仓库；
- 只有解析和显式检查操作读取现场仓库；
- 解析不执行 Git checkout/pull/reset/commit；
- dirty 仓库可以成功生成快照。

### 19.2 仓库独立性

- 一次选择任意仓库集合；
- 部分失败时成功仓库独立更新；
- 失败仓库保留旧 current；
- 可以只重试失败仓库；
- 未选择仓库完全不被访问。

### 19.3 一致性

- sync 期间源码变化会失败且不发布；
- SQLite/WAL 快照通过 integrity check；
- 历史快照在现场仓库修改或删除后仍可查询；
- 同一页面固定 View，不会混用新旧 node ID；
- 跨仓产物严格绑定 view digest。

### 19.4 可靠性

- 同一 member 不会并发 sync；
- 服务重启将遗留任务标记 interrupted；
- cancel 不影响旧 current；
- unchanged 不重复存储分析产物；
- 空间不足不会破坏已有快照。

### 19.5 生命周期

- current、pinned 和被引用快照不能被直接删除；
- 移除注册不会删除历史；
- retention 不删除受保护产物；
- label/note/pin 可修改，但快照内容不可修改。

### 19.6 旧能力移除

- CLI 不再暴露 backfill/update/演进 status；
- Web 不再暴露旧 init+backfill 流程；
- MCP 和 Chat 不再查询 EvolutionStore；
- 生产代码不再依赖旧 Git Evolution 模块；
- 已有 evolution.db 文件不会被安装、启动或升级过程删除。

## 20. 已确认决策清单

本设计中的以下决策均已由用户确认：

1. 完整可查询快照，而非只缓存报告 JSON；
2. latest 指最新成功快照，失败另行显示；
3. 页面固定 view ID；
4. 仓库独立发布，部分成功立即生效；
5. 不后台检测，只有显式“检查改动”；
6. 冻结 CodeGraph 索引源码和白名单配置；
7. 重试保留独立 Attempt；
8. member 级并发互斥；
9. unchanged 不复制结果；
10. 未解析成员显式进入 incomplete View；
11. 基础解析不自动调用 LLM；
12. 首次解析自动 CodeGraph init，之后每次 sync；
13. 当前解析不触发 Git 历史 backfill/update；
14. 使用稳定 member ID；
15. 默认最近 10 个成功快照和 30 天失败日志；
16. 敏感文件排除与用户目录权限；
17. 移除注册不删除历史；
18. 跨仓产物按 view digest 懒构建；
19. 旧规则和解释标记 legacy_unbound；
20. 完整删除旧 Evolution 子系统但保留磁盘数据；
21. 同步期间源码变化由用户手动重试；
22. 改动检查按实际分析输入摘要；
23. 普通 View 不持久化，被引用时才固定；
24. 服务重启不恢复 subprocess；
25. 支持 label、note、pin；
26. MCP 改为快照知识工具；
27. Chat 只查询快照；
28. 删除旧 Runtime Telemetry；
29. 第一阶段仍要求 Git 仓库，但允许 dirty；
30. 不保留旧接口兼容别名；
31. 解析期间移除成员时保存 orphaned 快照但不发布；
32. 被引用快照默认拒绝删除；
33. Run 可选择任意已注册成员；
34. 每次执行完整非 LLM 分析；
35. 支持仓库和 Run 维度取消；
36. 不跟随外部 symlink，submodule 单独注册；
37. 快照统一保存到 CodeEvolution 数据目录；
38. 空间不足时拒绝开始；
39. Evolution 未来表示手动快照之间的差异；
40. 单仓事实与组合事实分开；
41. 默认并发度为 2；
42. 区分源码变化与分析器过期；
43. 不自动导入现有 CodeGraph；
44. 快照内容不可变；
45. 外部上下文不透明保存；
46. 支持固定和导出组合；
47. 普通读取不访问现场仓库。

当前无未决产品问题。实施前仍需将每个阶段拆成可验证的工程任务，但不得改变上述行为
契约。
