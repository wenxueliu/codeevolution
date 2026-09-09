# 仓库级解析快照详细技术设计

> 状态：设计完成，待实施
>
> 日期：2026-09-09
>
> 上游方案：[仓库级解析快照设计](repository-analysis-snapshot-design.md)
>
> 适用范围：Repository Analysis Snapshot、Analysis Run、Graph View、快照绑定查询、旧 Evolution Engine 移除

## 1. 文档目的与约束

本文把上游方案中的产品决策细化为可直接拆分工程任务的技术设计，重点解决：

- 数据库、Artifact Store 与现场仓库之间的一致性边界；
- Run、Attempt、Snapshot、Graph View 的状态、身份和生命周期；
- Web、CLI、MCP、Chat、调用树和跨仓分析的快照绑定；
- 取消、重启、部分失败、删除、保留和磁盘不足时的确定行为；
- 从现有 live-read 架构迁移到 snapshot-only 架构的切换方式。

本文不实现代码。实施时若本文与上游方案存在细节冲突，以本文已经明确标注的“方案细化”
为准；产品目标和非目标仍以上游方案为准。

### 1.1 已确认的方案细化

1. 使用单服务进程内的持久调度器；CodeGraph 作为独立进程组执行，不引入外部队列。
2. 普通 Graph View 以 24 小时滑动 TTL 短期持久化；Pinned View 永久持久化。
3. `analysis-snapshots.db` 是仓库注册和快照生命周期的唯一运行时数据源。
4. 重试创建新 Run，并通过 `retry_of_attempt_id` 连接原 Attempt。
5. View 由显式成员集合定义；scope 只是解析成员集合的便捷 selector。
6. 快照保存完整、规范化的单仓事实；面向 UI 的截断和分页是读取投影。
7. CodeGraph 同时保存物理文件摘要和逻辑图摘要。
8. `.env`、密钥和证书等敏感文件既不冻结也不读取；依赖这些输入的结果降级并标记不完整。
9. Graph View 组合产物按持久任务异步、懒生成。
10. 发布事务开始前是取消的最后线性化点。
11. 使用 A/B/C 三次输入清单检查与冻结副本逐文件校验。
12. 删除和 Artifact GC 使用可恢复的两阶段流程。
13. `snapshot_references` 是删除保护的权威引用索引。
14. CLI 和 MCP 同样只读快照，不保留 live 分析入口。
15. 大于等于 1 MiB 的组合 JSON 产物写 Artifact Store，小结果允许内联。

## 2. 设计原则与系统不变量

以下不变量必须由数据库约束、端口边界和自动化测试共同保证：

1. **普通读取零现场访问**：除显式解析和显式检查外，任何 API、CLI、MCP、Chat 或
   后台缓存任务都不得访问注册路径、现场 `.codegraph` 或现场源码。
2. **仓库独立发布**：一个成员失败、取消或中断不回滚同 Run 中其他成员已经发布的结果。
3. **current 只指成功结果**：失败、取消、中断、冲突和发布失败都不能改变 current。
4. **快照内容不可变**：事实、证据、版本和 provenance 只插入不更新；用户元数据独立存储。
5. **页面组合稳定**：View 创建后，其成员集合和快照映射不可修改；刷新产生新 View。
6. **Artifact 先于元数据 durable**：数据库不能引用未完成、未校验或未 fsync 的 Artifact。
7. **一个成员最多一个活动 Attempt**：约束必须在 SQLite 中生效，不能只靠 Python 锁。
8. **分析只读冻结输入**：Knowledge、Topology、Rule、Explanation 等分析器不能收到现场路径。
9. **删除先证明无引用**：current、View、pin、规则和解释引用都必须阻止快照物理删除。
10. **摘要不混入展示信息**：capture time、绝对路径、label、note、Git branch 等不参与内容身份。
11. **敏感信息不进入证据**：secret 文件内容、环境变量值、token 和绝对用户路径不得进入
    report、manifest、API 错误或日志摘录。
12. **旧数据只读保留**：已有 `evolution.db` 不迁移、不打开、不删除、不继续写入。

## 3. 领域模型

### 3.1 限界上下文

| 上下文 | 负责 | 不负责 |
|---|---|---|
| Repository Catalog | Scope、Member 的稳定身份和注册属性 | 解析结果和版本兼容性 |
| Analysis Execution | Run、Attempt、调度、取消、重试和恢复 | 知识查询展示 |
| Snapshot Publication | 证据冻结、单仓事实、current 指针 | 多仓组合事实 |
| Graph View | 固定成员到快照的映射、组合完整性、pin/export | 改变仓库 current |
| Derived Knowledge | 单仓读取、跨仓 Artifact、Rule、Explanation、Chat | 现场仓库扫描 |
| Retention | 引用保护、逻辑删除、Artifact GC、空间预检 | 自动解析或自动更新 current |

### 3.2 术语和身份

| 术语 | 定义 | 稳定身份 |
|---|---|---|
| Analysis Scope | 仓库成员的逻辑分组，可改名 | `scope_id` |
| Repository Member | 一个已注册的物理 Git 仓库 | `member_id` |
| Analysis Run | 一次用户选择与执行意图 | `run_id` |
| Repository Attempt | Run 中一个 member 的一次执行 | `attempt_id` |
| Evidence Bundle | 冻结 CodeGraph、源码和 manifest 的内容寻址集合 | `evidence_digest` |
| Repository Snapshot | 某成员的一份不可变事实和证据绑定 | `snapshot_id` |
| Current Snapshot | 成员当前用于新 View 的最新成功 Snapshot | `(member_id, snapshot_id)` |
| Graph View | 不可变的成员集合及其 Snapshot 映射 | `view_id`、`view_digest` |
| View Artifact | 依赖一个 View 的跨仓派生结果 | `artifact_job_id`、cache key |
| Snapshot Reference | 阻止 Snapshot 被删除的权威引用 | `reference_id` |

关系如下：

```text
AnalysisScope 1 ── * RepositoryMember
RepositoryMember 1 ── * RepositoryAttempt * ── 1 AnalysisRun
RepositoryMember 1 ── * RepositorySnapshot * ── 1 EvidenceBundle
RepositoryMember 1 ── 0..1 CurrentSnapshot
GraphView 1 ── * GraphViewMember * ── 0..1 RepositorySnapshot
GraphView 1 ── * GraphViewArtifactJob
RepositorySnapshot 1 ── * SnapshotReference
```

`RepositorySnapshot` 表示单仓事实；`GraphViewArtifact` 表示组合事实。两者不可混用。

## 4. 当前实现差距

实施前必须识别并切断以下 live-read 路径：

| 现有路径 | 当前行为 | 目标行为 |
|---|---|---|
| `api.py:get_knowledge_service` | 打开注册路径下 CodeGraph DB | 注入 `SnapshotQueryService` |
| `KnowledgeService.from_codegraph` | 从 DB 路径反推源码根 | 显式接收 graph/source ports |
| `GroupedKnowledgeService.report` | 扫描现场前端源码 | 生成 `frontend-callers` View Artifact |
| `CallTreeService` | 按 repo/member 打开现场 DB | 接收已解析的 Snapshot Handle |
| `NodeRuleService` | 现场解析节点和源码 | 绑定 Snapshot 或 View |
| `CrossRepoImplementation` | 接收 `{name: path}` 并打开现场 DB/源码 | 接收 `ResolvedGraphView` |
| topology cache | 使用 DB mtime/size 判断新鲜度 | 使用完整 View Artifact cache key |
| API Explanation freshness | 重新读取现场生成证据 | 比较绑定 Snapshot 与 View 映射 |
| Chat | 查询 live graph 和 EvolutionStore | 只暴露 snapshot knowledge operations |
| CLI knowledge/topology/flow | 直接读取注册仓库 | 强制 Snapshot/View selector |
| Web init 状态 | 进程内线程和字典 | 持久 Run/Attempt 调度器 |

现有 `SourceProvider` 只有 `read_text` 和 `snippet`，不足以支持前端源码、配置和拓扑扫描。
目标端口必须增加 manifest-backed 文件枚举能力，禁止分析器自行接收 `Path`。

## 5. 目标架构与模块边界

依赖方向继续遵守：

```text
delivery → application → analysis/domain/ports ← infrastructure
```

### 5.1 新增或调整的模块

```text
codeevolution/
  domain/
    analysis_snapshot.py       # DTO、枚举、状态迁移和聚合规则
  ports.py                     # 新增 snapshot/capture/artifact/scheduler ports
  application/
    analysis_run_service.py    # 创建、重试、取消、查询 Run
    analysis_scheduler.py      # 有界调度和进程生命周期
    repository_attempt_worker.py
    repository_snapshot_service.py
    graph_view_service.py
    graph_artifact_service.py
    snapshot_retention_service.py
  infrastructure/
    analysis_snapshot_sqlite.py
    artifact_store_fs.py
    codegraph_command.py
    codegraph_capture.py
    workspace_input_scanner.py
    snapshot_source.py
    snapshot_bundle_resolver.py
```

现有 analysis 组件应逐步改为只接收端口；兼容 facade 在迁移期可以保留内部导入路径，
但不得保留 live 行为。

### 5.2 核心端口

```python
class SnapshotCatalog(Protocol):
    def create_run(...): ...
    def claim_next_attempt(...): ...
    def request_cancel(...): ...
    def publish_snapshot(...): ...
    def resolve_view(...): ...

class ArtifactStore(Protocol):
    def create_staging(...): ...
    def publish(...): ...
    def open(...): ...
    def move_to_trash(...): ...

class WorkspaceInputScanner(Protocol):
    def scan(member, policy) -> InputObservation: ...

class CodeGraphCommandRunner(Protocol):
    def init_or_sync(member, cancellation) -> CommandResult: ...

class SnapshotBundleResolver(Protocol):
    def open_snapshot(snapshot_id) -> RepositorySnapshotHandle: ...

class SourceInventory(Protocol):
    def list_files(categories=None, globs=None) -> list[SourceEntry]: ...
    def read_bytes(path) -> bytes | None: ...
    def read_text(path) -> str | None: ...
    def snippet(path, start, end) -> str | None: ...
```

`RepositorySnapshotHandle` 至少包含 `snapshot_id`、`member_id`、`CodeGraphRepository`、
`SourceInventory`、完整事实、manifest 和版本身份。打开 Handle 时必须验证 Artifact 存在、
manifest 摘要正确且 Snapshot 属于请求成员。

## 6. 存储拓扑

### 6.1 数据目录

新快照子系统只使用：

```text
${CODEEVOLUTION_DATA_DIR:-~/.codeevolution}/
  analysis-snapshots.db
  artifacts/sha256/<prefix>/<digest>/...
  staging/<attempt_id>/...
  trash/<deletion_id>/...
  migration-backups/registry-<timestamp>.json
```

该路径不得使用 `paths.data_dir()` 的 `.codehistory` 自动回退逻辑。应提供专用
`analysis_data_dir()`；只尊重显式 `CODEEVOLUTION_DATA_DIR` 或 `~/.codeevolution`。

目录权限为 `0700`，普通文件为 `0600`，进程启动时使用等价于 `umask 077` 的权限策略。

### 6.2 SQLite 配置

每个 worker 使用独立连接：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

schema 使用 `PRAGMA user_version` 逐版迁移。迁移必须在服务接收请求和调度器启动之前，
使用独占迁移事务；迁移失败时服务不启动任务处理，但不能改动旧 `evolution.db`。

## 7. 数据库模型

以下 DDL 表达逻辑约束；具体迁移可按 SQLite 支持拆分 trigger 和 rebuild table。

### 7.1 Catalog

```sql
CREATE TABLE analysis_scopes (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    retired_at      TEXT
);

CREATE TABLE repository_members (
    id              TEXT PRIMARY KEY,
    scope_id        TEXT NOT NULL REFERENCES analysis_scopes(id) ON DELETE RESTRICT,
    display_name    TEXT NOT NULL,
    registered_path TEXT NOT NULL,
    path_identity   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    retired_at      TEXT,
    UNIQUE(scope_id, display_name)
);

CREATE INDEX idx_members_scope_active
ON repository_members(scope_id, retired_at);

CREATE UNIQUE INDEX uq_active_member_path
ON repository_members(path_identity) WHERE retired_at IS NULL;
```

`path_identity` 是规范化路径的本机身份，仅用于识别重复注册和发布时验证路径未被换绑；
它不进入 Snapshot 或 View digest。成员删除是 retire，不做级联删除。

### 7.2 Run、Attempt 与观察记录

```sql
CREATE TABLE analysis_runs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL CHECK(status IN
                      ('pending','running','completed','partial','failed',
                       'cancelled','interrupted')),
    requested_at      TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT,
    external_context  TEXT NOT NULL DEFAULT '{}',
    tags_json         TEXT NOT NULL DEFAULT '[]',
    idempotency_key   TEXT,
    CHECK(length(external_context) <= 16384),
    CHECK(length(tags_json) <= 4096)
);

CREATE UNIQUE INDEX uq_runs_idempotency
ON analysis_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE analysis_run_members (
    run_id            TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    member_id         TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    ordinal           INTEGER NOT NULL,
    disposition       TEXT NOT NULL CHECK(disposition IN ('queued','already_running')),
    attempt_id        TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    blocking_attempt_id TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    PRIMARY KEY(run_id, member_id),
    CHECK((disposition='queued' AND attempt_id IS NOT NULL AND blocking_attempt_id IS NULL)
       OR (disposition='already_running' AND attempt_id IS NULL AND blocking_attempt_id IS NOT NULL))
);

CREATE TABLE repository_attempts (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    member_id             TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    attempt_no            INTEGER NOT NULL,
    status                TEXT NOT NULL CHECK(status IN
                          ('pending','running','completed','unchanged','failed',
                           'cancelled','interrupted')),
    stage                 TEXT NOT NULL CHECK(stage IN
                          ('queued','validating','digest_before','codegraph_init',
                           'codegraph_sync','digest_after','freezing_graph',
                           'freezing_sources','digest_final','analyzing','publishing','finished')),
    requested_at          TEXT NOT NULL,
    started_at            TEXT,
    completed_at          TEXT,
    retry_of_attempt_id   TEXT REFERENCES repository_attempts(id) ON DELETE RESTRICT,
    blocking_attempt_id   TEXT REFERENCES repository_attempts(id) ON DELETE SET NULL,
    snapshot_id           TEXT REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    cancellation_requested_at TEXT,
    worker_instance_id    TEXT,
    progress_json         TEXT NOT NULL DEFAULT '{}',
    error_code            TEXT,
    error_message         TEXT,
    error_details_json    TEXT NOT NULL DEFAULT '{}',
    log_excerpt           TEXT,
    UNIQUE(member_id, attempt_no)
);

CREATE UNIQUE INDEX uq_active_attempt_per_member
ON repository_attempts(member_id)
WHERE status IN ('pending','running');

CREATE INDEX idx_attempts_run ON repository_attempts(run_id);
CREATE INDEX idx_attempts_member_time ON repository_attempts(member_id, requested_at DESC);

CREATE TABLE attempt_observations (
    id                    TEXT PRIMARY KEY,
    attempt_id            TEXT NOT NULL REFERENCES repository_attempts(id) ON DELETE CASCADE,
    phase                 TEXT NOT NULL CHECK(phase IN ('before_sync','after_sync','after_freeze')),
    observed_at           TEXT NOT NULL,
    input_digest          TEXT NOT NULL,
    observed_branch       TEXT,
    observed_head_commit  TEXT,
    dirty                 INTEGER NOT NULL CHECK(dirty IN (0,1)),
    file_count            INTEGER NOT NULL,
    total_bytes           INTEGER NOT NULL,
    UNIQUE(attempt_id, phase)
);

CREATE TABLE member_change_checks (
    member_id             TEXT PRIMARY KEY REFERENCES repository_members(id) ON DELETE RESTRICT,
    status                TEXT NOT NULL CHECK(status IN
                          ('unchanged','source_changed','analyzer_outdated','unparsed','check_failed')),
    checked_at            TEXT NOT NULL,
    input_digest          TEXT,
    observed_branch       TEXT,
    observed_head_commit  TEXT,
    dirty                 INTEGER CHECK(dirty IN (0,1)),
    error_code            TEXT,
    error_message         TEXT
);
```

遇到已经活动的 member 时，请求仍在 Run 的结果中返回 disposition
`already_running` 和 `blocking_attempt_id`，但不插入第二个活动 Attempt。`already_running`
不是 Attempt 终态，避免污染状态机和唯一索引。`analysis_run_members` 固定保存请求 disposition、
本 Run Attempt ID 或 blocking Attempt ID；创建后不可改写成员集合。

### 7.3 Evidence、Snapshot 与 current

```sql
CREATE TABLE evidence_bundles (
    digest                  TEXT PRIMARY KEY,
    artifact_key            TEXT NOT NULL UNIQUE,
    manifest_schema_version TEXT NOT NULL,
    source_digest           TEXT NOT NULL,
    graph_digest            TEXT NOT NULL,
    graph_blob_sha256       TEXT NOT NULL,
    byte_size               INTEGER NOT NULL CHECK(byte_size >= 0),
    capture_policy_digest   TEXT NOT NULL,
    capture_completeness    TEXT NOT NULL CHECK(capture_completeness IN ('complete','incomplete')),
    created_at              TEXT NOT NULL,
    deletion_state          TEXT NOT NULL DEFAULT 'active'
                            CHECK(deletion_state IN ('active','deletion_pending','trashed'))
);

CREATE TABLE repository_analysis_snapshots (
    id                       TEXT PRIMARY KEY,
    member_id                TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    evidence_digest          TEXT NOT NULL REFERENCES evidence_bundles(digest) ON DELETE RESTRICT,
    captured_at              TEXT NOT NULL,
    observed_branch          TEXT,
    observed_head_commit     TEXT,
    dirty                    INTEGER NOT NULL CHECK(dirty IN (0,1)),
    codegraph_version        TEXT NOT NULL,
    codegraph_schema_digest  TEXT NOT NULL,
    analyzer_bundle_digest   TEXT NOT NULL,
    rules_digest             TEXT NOT NULL,
    report_schema_version    TEXT NOT NULL,
    options_digest           TEXT NOT NULL,
    facts_digest             TEXT NOT NULL,
    facts_storage            TEXT NOT NULL CHECK(facts_storage IN ('inline','artifact')),
    facts_json               TEXT,
    facts_artifact_key       TEXT,
    analysis_completeness    TEXT NOT NULL CHECK(analysis_completeness IN ('complete','incomplete')),
    orphaned                 INTEGER NOT NULL DEFAULT 0 CHECK(orphaned IN (0,1)),
    deletion_state           TEXT NOT NULL DEFAULT 'active'
                             CHECK(deletion_state IN ('active','deletion_pending','trashed')),
    CHECK((facts_storage='inline' AND facts_json IS NOT NULL AND facts_artifact_key IS NULL)
       OR (facts_storage='artifact' AND facts_json IS NULL AND facts_artifact_key IS NOT NULL)),
    UNIQUE(member_id, evidence_digest, analyzer_bundle_digest, rules_digest,
           report_schema_version, options_digest)
);

CREATE TABLE repository_snapshot_metadata (
    snapshot_id       TEXT PRIMARY KEY REFERENCES repository_analysis_snapshots(id) ON DELETE CASCADE,
    label             TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT '',
    pinned            INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
    metadata_version  INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT NOT NULL
);

CREATE TABLE current_repository_snapshots (
    member_id         TEXT PRIMARY KEY REFERENCES repository_members(id) ON DELETE RESTRICT,
    snapshot_id       TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    published_at      TEXT NOT NULL
);

CREATE TRIGGER trg_current_snapshot_member_insert
BEFORE INSERT ON current_repository_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM repository_analysis_snapshots s
        JOIN evidence_bundles e ON e.digest = s.evidence_digest
        WHERE s.id = NEW.snapshot_id AND s.member_id = NEW.member_id
          AND s.orphaned = 0 AND s.deletion_state = 'active'
          AND e.deletion_state = 'active'
    ) THEN RAISE(ABORT, 'current_snapshot_member_mismatch') END;
END;

CREATE TRIGGER trg_current_snapshot_member_update
BEFORE UPDATE ON current_repository_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM repository_analysis_snapshots s
        JOIN evidence_bundles e ON e.digest = s.evidence_digest
        WHERE s.id = NEW.snapshot_id AND s.member_id = NEW.member_id
          AND s.orphaned = 0 AND s.deletion_state = 'active'
          AND e.deletion_state = 'active'
    ) THEN RAISE(ABORT, 'current_snapshot_member_mismatch') END;
END;

CREATE TRIGGER trg_snapshot_cannot_invalidate_current
BEFORE UPDATE OF deletion_state, orphaned ON repository_analysis_snapshots
WHEN (NEW.deletion_state != 'active' OR NEW.orphaned != 0)
 AND EXISTS (SELECT 1 FROM current_repository_snapshots c WHERE c.snapshot_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'current_snapshot_cannot_be_invalidated');
END;

CREATE TRIGGER trg_evidence_cannot_invalidate_current
BEFORE UPDATE OF deletion_state ON evidence_bundles
WHEN NEW.deletion_state != 'active'
 AND EXISTS (
     SELECT 1 FROM repository_analysis_snapshots s
     JOIN current_repository_snapshots c ON c.snapshot_id = s.id
     WHERE s.evidence_digest = NEW.digest
 )
BEGIN
    SELECT RAISE(ABORT, 'current_evidence_cannot_be_invalidated');
END;
```

应用事务和 trigger 共同验证 current 的 `member_id` 与 Snapshot 的 `member_id` 相同。内容表
不提供通用 update repository；除受控的 `deletion_state` 生命周期更新外，只有 metadata 表允许
乐观并发更新。

### 7.4 Graph View

```sql
CREATE TABLE graph_views (
    id                TEXT PRIMARY KEY,
    digest            TEXT NOT NULL,
    lifecycle         TEXT NOT NULL CHECK(lifecycle IN ('ephemeral','pinned')),
    completeness      TEXT NOT NULL CHECK(completeness IN ('complete','incomplete')),
    created_at        TEXT NOT NULL,
    last_accessed_at  TEXT NOT NULL,
    expires_at        TEXT,
    selector_json     TEXT NOT NULL,
    label             TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT '',
    external_context  TEXT NOT NULL DEFAULT '{}',
    tags_json         TEXT NOT NULL DEFAULT '[]',
    CHECK((lifecycle='ephemeral' AND expires_at IS NOT NULL)
       OR (lifecycle='pinned' AND expires_at IS NULL)),
    CHECK(length(external_context) <= 16384),
    CHECK(length(tags_json) <= 4096)
);

CREATE INDEX idx_views_digest ON graph_views(digest);
CREATE INDEX idx_views_expiry ON graph_views(lifecycle, expires_at);

CREATE TABLE graph_view_members (
    view_id           TEXT NOT NULL REFERENCES graph_views(id) ON DELETE CASCADE,
    ordinal           INTEGER NOT NULL,
    member_id         TEXT NOT NULL REFERENCES repository_members(id) ON DELETE RESTRICT,
    snapshot_id       TEXT REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    availability      TEXT NOT NULL CHECK(availability IN ('available','unparsed','retired')),
    PRIMARY KEY(view_id, member_id),
    UNIQUE(view_id, ordinal),
    CHECK((availability='available' AND snapshot_id IS NOT NULL)
       OR (availability!='available' AND snapshot_id IS NULL))
);
```

相同 digest 可以有多个 View ID，因为生命周期、用户元数据和 TTL 不属于内容身份。创建 View
时可复用仍有效且 selector 等价的 ephemeral View，但调用方不能依赖复用。

### 7.5 View Artifact 任务与缓存

```sql
CREATE TABLE graph_view_artifact_cache (
    cache_key_digest         TEXT PRIMARY KEY,
    view_digest              TEXT NOT NULL,
    artifact_kind            TEXT NOT NULL,
    params_digest            TEXT NOT NULL,
    analyzer_bundle_digest   TEXT NOT NULL,
    rules_digest             TEXT NOT NULL,
    artifact_schema_version  TEXT NOT NULL,
    semantic_identity_digest TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL,
    last_accessed_at         TEXT NOT NULL,
    payload_storage          TEXT NOT NULL CHECK(payload_storage IN ('inline','artifact')),
    payload_json             TEXT,
    artifact_key             TEXT,
    payload_digest           TEXT NOT NULL,
    byte_size                INTEGER NOT NULL CHECK(byte_size >= 0),
    CHECK((payload_storage='inline' AND payload_json IS NOT NULL AND artifact_key IS NULL)
       OR (payload_storage='artifact' AND payload_json IS NULL AND artifact_key IS NOT NULL)),
    UNIQUE(view_digest, artifact_kind, params_digest, analyzer_bundle_digest,
           rules_digest, artifact_schema_version, semantic_identity_digest)
);

CREATE TABLE graph_view_artifact_jobs (
    id                       TEXT PRIMARY KEY,
    view_id                  TEXT REFERENCES graph_views(id) ON DELETE SET NULL,
    cache_key_digest         TEXT NOT NULL,
    attempt_no               INTEGER NOT NULL,
    retry_of_job_id          TEXT REFERENCES graph_view_artifact_jobs(id) ON DELETE RESTRICT,
    status                   TEXT NOT NULL CHECK(status IN
                             ('pending','running','completed','failed','cancelled','interrupted')),
    requested_at             TEXT NOT NULL,
    started_at               TEXT,
    completed_at             TEXT,
    error_code               TEXT,
    error_message            TEXT,
    UNIQUE(cache_key_digest, attempt_no)
);

CREATE UNIQUE INDEX uq_active_graph_artifact_job
ON graph_view_artifact_jobs(cache_key_digest)
WHERE status IN ('pending','running');
```

`params_digest` 必须区分 impact 的目标 service、flow 的入口和深度、entities 的选项等。
LLM 产物的 `semantic_identity_digest` 包含 provider、model、prompt、输出 schema 和输入摘要；
确定性产物使用空字符串。cache 与具体 `view_id` 解耦，同 digest 的不同 View 可以复用结果；job
允许按 `attempt_no` 重试。job 活动期间用 `artifact_job` reference 保护输入 Snapshot，进入终态后
释放该引用；`view_id ON DELETE SET NULL` 保证终态审计记录不会阻止 ephemeral View 到期清理。

### 7.6 引用、删除和空间预留

```sql
CREATE TABLE snapshot_references (
    id                TEXT PRIMARY KEY,
    snapshot_id       TEXT NOT NULL REFERENCES repository_analysis_snapshots(id) ON DELETE RESTRICT,
    owner_type        TEXT NOT NULL CHECK(owner_type IN
                      ('current','ephemeral_view','pinned_view','snapshot_pin',
                       'node_rule','business_rule','api_explanation','artifact_job')),
    owner_id          TEXT NOT NULL,
    state             TEXT NOT NULL CHECK(state IN ('temporary','permanent')),
    created_at        TEXT NOT NULL,
    expires_at        TEXT,
    UNIQUE(snapshot_id, owner_type, owner_id)
);

CREATE INDEX idx_snapshot_refs_snapshot ON snapshot_references(snapshot_id, state, expires_at);

CREATE TABLE storage_reservations (
    attempt_id        TEXT PRIMARY KEY REFERENCES repository_attempts(id) ON DELETE CASCADE,
    reserved_bytes    INTEGER NOT NULL CHECK(reserved_bytes >= 0),
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL
);

CREATE TABLE deletion_jobs (
    id                TEXT PRIMARY KEY,
    target_type       TEXT NOT NULL CHECK(target_type IN ('snapshot','evidence','view_artifact')),
    target_id         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK(status IN ('pending','trashed','completed','failed')),
    requested_at      TEXT NOT NULL,
    completed_at      TEXT,
    trash_path        TEXT,
    error_message     TEXT
);
```

规则和解释可以继续拥有专用 payload store，但创建任务必须通过应用服务先写
`snapshot_references`。生成失败或取消后删除临时引用；成功发布后在事务中转为永久引用。
legacy_unbound 记录不创建引用，也不能被推断绑定。

## 8. 内容身份、Manifest 与规范化

### 8.1 摘要层次

| 摘要 | 输入 | 用途 |
|---|---|---|
| `source_digest` | 排序后的冻结源文件条目 | 判断源码输入变化 |
| `graph_blob_sha256` | `codegraph.db` 原始字节 | 完整性与 CAS |
| `graph_digest` | 规范化图节点、边和 files | 判断图语义变化 |
| `facts_digest` | canonical 完整单仓事实 | 验证结果和缓存 |
| `evidence_digest` | 仅含冻结证据和 capture policy 的 manifest body | Evidence Bundle 内容身份 |
| `view_digest` | 固定成员集合及映射 | Graph View 内容身份 |
| `params_digest` | 含默认值的有效参数 | View Artifact 缓存隔离 |

### 8.2 Canonical JSON

所有摘要使用 UTF-8 JSON：对象键按 Unicode code point 排序、无多余空白、禁止 NaN/Infinity、
时间统一 RFC3339 UTC。路径先转 POSIX 相对路径并做 Unicode NFC 规范化。集合型数组按定义的
稳定键排序；顺序有业务意义的数组保留顺序。

### 8.3 Source digest

每个 included 条目规范化为：

```json
{"path":"src/order.py","sha256":"...","size":1234,"category":["indexed","topology"]}
```

对按 `path` 排序后的条目数组计算 SHA-256。mtime、owner、绝对路径、Git branch 和 capture
time 不参与摘要。文件内容使用原始 bytes 计算；文本解码状态另存 manifest。

### 8.4 Logical graph digest

`graph_blob_sha256` 不能代表图语义。`graph_digest` 应：

1. 对 CodeGraph schema 生成 `schema_fingerprint`；
2. 将 node 映射为稳定语义键，例如
   `(kind, qualified_name, normalized_file_path, start_line, signature)`；
3. edge 使用 source/target 稳定语义键、kind 和规范化 metadata，不使用 SQLite 自增 ID；
4. files 使用 `(path, content_hash, language, size)`；
5. 各集合排序后统一哈希。

若某语言无法形成唯一 node 语义键，加入稳定的同键序号并在 manifest 记录 collision count。
CodeGraph 版本或 schema 变化即使逻辑摘要相同，也仍由 Snapshot identity tuple 触发重新分析。

### 8.5 Evidence manifest

```json
{
  "manifest_schema_version": "1",
  "capture_policy_digest": "sha256:...",
  "codegraph": {
    "path": "codegraph.db",
    "blob_sha256": "sha256:...",
    "logical_graph_digest": "sha256:...",
    "byte_size": 0,
    "schema_fingerprint": "sha256:...",
    "integrity_check": "ok"
  },
  "sources": [
    {
      "path": "src/order.py",
      "sha256": "sha256:...",
      "size": 0,
      "category": ["indexed"],
      "encoding_status": "utf8"
    }
  ],
  "exclusions": [
    {"path": ".env", "reason": "secret_file", "required_by_analyzer": "config"}
  ],
  "digests": {
    "source": "sha256:...",
    "graph": "sha256:..."
  },
  "capture_completeness": {
    "status": "incomplete",
    "reasons": ["secret_file_excluded"]
  }
}
```

manifest 不包含自身 digest、绝对注册路径、member ID、Git 状态或用户元数据，从而允许同一
Evidence Bundle 被安全复用。它也不包含 analyzer、rules、options 或 report schema 身份；这些
属于 Snapshot facts derivation，保存在 Snapshot 行和 facts manifest 中。provenance 保存在
Snapshot 和 Attempt 表。

### 8.6 分析器身份

`analyzer_bundle_digest` 不能只使用包版本 `0.1.0`。它至少包含：

- 每个 extractor 的算法版本；
- 影响输出的依赖版本；
- 确定性随机 seed；
- report builder 版本；
- source capture policy 版本。

`rules_digest` 由实际规则内容生成；`options_digest` 使用补齐默认值后的有效选项。Snapshot
身份 tuple 为：

```text
(member_id, evidence_digest, analyzer_bundle_digest,
 rules_digest, report_schema_version, options_digest)
```

`capture_policy_digest` 单独描述“哪些文件可成为证据”的策略版本，属于 Evidence identity。
Evidence 的 `capture_completeness` 只表示冻结过程是否排除了候选证据；Snapshot 的
`analysis_completeness` 由当前 analyzer 对 exclusions 的 required/optional 判定生成。分析器升级时
可以复用同一 Evidence，但重新计算 analysis completeness 和 facts。

## 9. 输入发现、冻结和安全策略

### 9.1 候选输入集合

首次没有 CodeGraph DB 时也必须能扫描输入。`WorkspaceInputScanner` 使用与当前 CodeGraph 版本
匹配的语言/扩展策略发现“可索引源码”，而不是把整个 Git 仓库作为输入。现场观察集合为：

```text
Git tracked 中的可索引源码
∪ Git non-ignored untracked 中的可索引源码
∪ 分析器声明的白名单配置、拓扑和前端文件
```

随后应用统一排除策略。Git 只负责枚举 tracked/non-ignored 状态，不意味着所有 Git 文件都被
读取或冻结。不得仅依赖 CodeGraph `files`，否则首次 init 前无法形成 A 摘要，也会漏掉配置和
未跟踪的可索引输入。

实际冻结集合严格为：

```text
sync 后 CodeGraph files 表覆盖的源码
∪ 分析器声明且实际会读取的白名单配置/拓扑/前端文件
```

可索引但最终未进入 CodeGraph files、且未被分析器声明的文件只参与 A/B/C 的变化检测，不进入
Evidence。manifest 记录数量与原因，便于诊断索引覆盖，但不保存其内容。

### 9.2 排除策略

无条件排除：

- `.git/**`、`.codegraph/**` 和 CodeEvolution 自身数据目录；
- `.env`、`.env.*`、私钥、证书、credential、token 等已知 secret 文件；
- Git ignored 文件；
- 指向根目录外的 symlink、socket、device、FIFO 和其他非普通文件；
- 未独立注册的 submodule 内容；
- 超过配置上限的文件、二进制和大型生成产物。

所有路径必须拒绝绝对路径、`..`、NUL 和 symlink escape。复制时使用防 TOCTOU 的文件打开
策略；发现 symlink swap 或 inode/type 变化则失败。

当被排除输入是某分析器声明的 required input 时，允许生成快照，但结果包含明确的
Evidence 包含 `capture_completeness=incomplete`；若排除项是当前分析器的 required input，Snapshot
同时包含 `analysis_completeness=incomplete` 和原因。分析器只能降级到 `unknown`、
`redis:default` 等安全结果，不得回读现场或把 secret 值放进输出。

### 9.3 A/B/C 一致性协议

```text
A = CodeGraph init/sync 前的现场观察集合摘要
B = init/sync 子进程退出后的现场观察集合摘要
冻结 CodeGraph 和源码
C = 冻结完成后的现场观察集合摘要
E = 根据 B 和冻结后的 CodeGraph files 计算的期望冻结集合
F = 冻结副本逐文件重算得到的冻结集合

发布前置条件：A == B == C，且 E == F
```

现场比较覆盖观察集合的文件集合、类型、大小和内容摘要；冻结比较覆盖实际证据集合，不能只比较
Git dirty、mtime 或已存在文件。
任一不等即 `source_changed_during_capture`，删除或回收 staging，不创建 Snapshot，不改变 current。

Git branch、HEAD 和 dirty 在 A/B/C 中记录为 provenance，但不参与 `source_digest`。如果 Git
状态变化对应的实际输入内容未变化，可保留最后观察值；如果路径身份改变则失败。

## 10. Analysis Run 与调度器

### 10.1 进程模型

- 一个 CodeEvolution 服务实例是唯一活动调度器；首期不支持多个服务进程共同消费任务。
- 调度器位于 FastAPI lifespan 内，在数据库迁移与恢复完成后启动。
- 默认全局并发度为 2，可配置；每个 member 的活动 Attempt 仍由 partial unique index限制为 1。
- CodeGraph 使用 `Popen(start_new_session=True)` 或平台等价方式启动独立进程组。
- 服务不承诺跨重启恢复子进程；重启后由用户显式重试。

若未来支持多服务实例，必须先增加 durable lease、heartbeat 和 fencing token；不能只移除
“单实例”检查。

### 10.2 创建 Run

1. 校验 `member_ids` 非空、去重、全部存在且未 retired；否则整个请求返回 422。
2. 校验 `external_context` 是 JSON object，canonical 后不超过 16 KiB；tags 是去重字符串数组，
   最多 32 项、每项最多 64 个 Unicode 字符，canonical 后不超过 4 KiB。
3. `BEGIN IMMEDIATE` 创建 Run。
4. 对无活动任务的 member 先创建 pending Attempt，再写入带 `attempt_id` 的 queued run-member。
5. 对有活动任务的 member 写入带 `blocking_attempt_id` 的 already_running run-member。
6. 事务提交时一次固定完整 `analysis_run_members`；冲突成员不使其他成员停止排队。

可选 `idempotency_key` 只保证创建请求幂等，不参与 Snapshot identity。

### 10.3 Attempt 状态机

`status` 表达生命周期，`stage` 表达当前步骤，不得混用：

```text
pending ──claim──> running ──success──> completed
                         ├──same identity──> unchanged
                         ├──error─────────> failed
                         ├──cancel────────> cancelled
                         └──restart───────> interrupted

pending ──cancel────────> cancelled
pending ──restart───────> interrupted
```

合法 stage 顺序：

```text
queued → validating → digest_before → codegraph_init|codegraph_sync
       → digest_after → freezing_graph → freezing_sources → digest_final
       → analyzing → publishing → finished
```

状态更新使用 CAS，例如 `UPDATE ... WHERE id=? AND status='running' AND stage=?`。终态不可重新
打开。重试创建新 Run 和新 Attempt，`retry_of_attempt_id` 指向原终态 Attempt。

### 10.4 Run 汇总规则

Run 先为每个固定 member 计算 effective outcome：queued disposition 取本 Run Attempt 的 status；
already_running disposition 直接映射为 `blocked`。`unchanged` 与 `completed` 都映射为 `success`。

随后按以下完备函数汇总：

```text
if any outcome == running:
    run = running
elif any outcome == pending:
    run = running if any attempt has started else pending
elif all outcome == success:
    run = completed
elif any outcome == success:
    run = partial
elif any outcome == failed:
    run = failed
elif any outcome == interrupted:
    run = interrupted
elif all outcome == cancelled:
    run = cancelled
else:
    # 剩余组合只含 cancelled 和 blocked，或全部 blocked
    run = failed
```

因此 `cancelled + blocked`、`interrupted + blocked` 等组合也有唯一结果；后者按照上述优先级为
interrupted。Run details 保留 blocked member 的 blocking Run/Attempt ID，区别于解析失败。
`completed_at` 只在首次进入终态时设置，不因重试改变。

### 10.5 取消与发布线性化

- pending Attempt：事务内直接改为 cancelled。
- running Attempt：先写 `cancellation_requested_at`，worker 在每个阶段边界检查。
- CodeGraph 子进程：向整个进程组发送 SIGTERM，等待配置超时，再 SIGKILL/平台等价动作并 wait。
- 分析器纯 Python 阶段：使用 cooperative cancellation token；长循环必须设检查点。
- publishing 事务开始前进行最后一次取消检查。
- 一旦 publishing 事务开始，发布获胜，取消请求返回 Attempt 已不可取消；最终为 completed/unchanged。

取消 Run 只请求取消其中未终结成员，已经发布的 Snapshot 不回滚。

### 10.6 服务重启恢复

服务接收流量前，在单个事务中：

1. 将所有 pending/running Attempt 标为 interrupted；
2. 保留 stage、progress、observations 和脱敏日志；
3. 释放 storage reservation；
4. 重算受影响 Run 状态；
5. 将旧 staging 标记为待清理，但不尝试恢复 subprocess。

这依赖“仅一个活动服务实例”的部署约束。进程锁用于拒绝第二个服务实例启动调度器。

## 11. 单仓 Attempt 详细流程

```text
claim member
  → validate member/path/git/path_identity
  → reserve disk capacity
  → observation A
  → codegraph init or sync
  → wait subprocess exit
  → observation B
  → SQLite backup into staging
  → integrity_check == ok
  → compute physical and logical graph digests
  → freeze allowed sources into staging
  → observation C and frozen digest F
  → require A == B == C and expected frozen set E == frozen set F
  → build SnapshotHandle from staging only
  → run complete deterministic non-LLM analysis
  → canonicalize facts and compute identity
  → unchanged or durable artifact publish
  → short database publish transaction
  → release reservation and member claim
```

### 11.1 CodeGraph 冻结

CodeGraph 子进程完全退出并被 wait 后，再连接源 SQLite。使用 SQLite backup API 写 staging
目标库，不要求也不依赖裸拷贝 WAL/SHM。目标库执行 `PRAGMA integrity_check`，唯一成功结果
是单行 `ok`。失败不得发布。

### 11.2 分析输入

分析器从 staging 构造 `SQLiteCodeGraphRepository` 与 `SnapshotSourceProvider`。完整非 LLM
单仓维度全部执行。前端调用者关联、Topology、Flow、Impact、跨服务实体对齐不属于单仓事实，
由 View Artifact 生成。

### 11.3 unchanged

如果 Snapshot identity tuple 已存在：

- Attempt 状态为 `unchanged`；
- `snapshot_id` 指向已有 Snapshot；
- 不重复写 Evidence 或 facts；
- current 已指向该 Snapshot 时不更新 `published_at`；
- current 指向其他 Snapshot 时可将其切回已有 Snapshot，但必须记录本次 Attempt 和 provenance。

仅 source 相同但分析器、规则、options 或 report schema 变化时，不是 unchanged；允许复用 Evidence
并生成新 Snapshot facts。

### 11.4 发布事务

Artifact 发布顺序：

1. staging 写完所有文件和 manifest；
2. 校验摘要、SQLite integrity 和 manifest 内部一致性；
3. fsync 文件、子目录和 staging 目录；
4. 在同文件系统原子 rename 到 CAS 目标；目标已存在时校验后复用；
5. fsync CAS 父目录；
6. `BEGIN IMMEDIATE`：重新校验 member 状态与取消标记；插入/复用 Evidence 和 Snapshot；
   写 metadata；写 current/reference；终结 Attempt；重算 Run；提交。

若 member 已 retired 或 path identity 已被移除，Snapshot 可保存为 `orphaned=1`，Attempt 标记
completed，但不得更新 current。若 Artifact rename 成功而 DB 事务失败，形成可回收 orphan，
由 scavenger 在安全年龄后清理。

## 12. Graph View

### 12.1 创建语义

`POST /api/graph-views/current` 请求必须提供一个 selector：

```json
{"scope_ids":["scope-1"]}
```

或：

```json
{"member_ids":["member-a","member-b"]}
```

服务在创建事务中把 selector 解析为排序稳定的明确成员集合，并读取各成员 current。成员没有
current 时仍写入 View，availability=`unparsed`；已被 retire 但显式选择的成员为 `retired`。

知识页面使用当前 scope；全局 topology 页面显式选择所有 active members。后续 scope 改名、
成员迁移或 current 变化都不修改已有 View。

历史浏览、固定组合和导入外部清单使用 `POST /api/graph-views`，提交显式映射：

```json
{
  "members": [
    {"member_id": "member-a", "snapshot_id": "snapshot-a-old"},
    {"member_id": "member-b", "snapshot_id": null, "availability": "unparsed"}
  ]
}
```

服务验证每个 Snapshot 属于对应 member 且仍可读。显式映射可以引用 retired member 的历史
Snapshot，此时 availability 仍为 `available`；`retired` 仅表示 selector 选中了 retired member 但
没有指定可用 Snapshot。历史 Snapshot View 与 current View 遵守同一 TTL、pin 和引用保护规则。

### 12.2 View digest

规范输入包含 namespace/schema、完整成员集合、snapshot ID 和 availability：

```json
{
  "schema":"graph-view/v1",
  "members":[
    {"member_id":"member-a","snapshot_id":"snapshot-a1","availability":"available"},
    {"member_id":"member-b","snapshot_id":null,"availability":"unparsed"}
  ]
}
```

即使新增成员没有 Snapshot，也会改变 digest。selector、label、TTL、external context 不进入 digest。

### 12.3 Ephemeral View 生命周期

- 创建时 `expires_at=now+24h`；
- 每次成功读取最多节流为每小时一次更新 `last_accessed_at` 和滑动 TTL；
- 创建 View 时为可用 Snapshot 写 `ephemeral_view` temporary references；
- TTL 到期且无正在执行的 Artifact/Rule/Explanation 任务时，删除 references 和 View；
- 页面持有已过期 View 时返回 410 `view_expired`，前端提示创建最新 View；
- pin 将 lifecycle 原子改为 pinned、清空 expires_at，并把引用转为 permanent。

分享、导出、规则或解释若需要长期可复现，必须 pin。Pin 不改变 View digest；GET 请求不会隐式
pin 或产生其他持久副作用。

### 12.4 查询校验

所有仓库级查询接收 `view_id + repository_snapshot_id` 时必须验证：

1. View 存在且未过期；
2. Snapshot 在 View 中属于目标 member；
3. availability 是 available；
4. Snapshot 未处于 deletion_pending/trashed；
5. Evidence 可打开并通过轻量 manifest 校验。

不匹配返回 409 `snapshot_view_mismatch`，绝不能回退 current 或现场仓库。

## 13. Snapshot-only 读取模型

### 13.1 单仓与聚合知识

- 单仓：从 Snapshot 完整 facts 读取，按 API 参数投影和分页。
- 聚合：按 View 成员顺序合并各 Snapshot facts。
- unparsed/retired 成员以显式占位和 completeness 原因返回，不静默过滤。
- 请求不触发 CodeGraph、Git、文件扫描、分析或 LLM。

canonical API facts 必须保存 `member_id`、`repository_snapshot_id`、稳定 endpoint key、HTTP method/path、
`handler_node_id` 和 handler location。这样调用树根可由知识响应直接取得；不允许在点击时回现场
重新解析 endpoint。其他可展开事实同样保存其 Snapshot 内 root node ID。

### 13.2 调用树

节点 ID 只在 Snapshot 内有效。调用树节点主键为：

```text
(repository_snapshot_id, codegraph_node_id)
```

跨服务 child 必须返回目标 `member_id`、`repository_snapshot_id` 和 `node_id`。前端缓存与
cycle key 使用 `(view_digest, snapshot_id, node_id)`，避免不同 Snapshot 的 node ID 串用。

### 13.3 Node Rule、Business Rule 与 API Explanation

- 新生成记录绑定 `repository_snapshot_id`；跨仓 subject 还要绑定 `view_id`。
- 服务端从 Snapshot/View 重建 prompt 输入，不信任客户端提交的 Mermaid 或调用链事实。
- 每次生成创建不可变 candidate；成功后用独立 current pointer 切换，不原地覆盖旧结果。
- 输入摘要包含 Snapshot/View、subject、model、prompt 和输出 schema。
- freshness 只比较绑定 Snapshot 与页面 View 中同成员 Snapshot：相同为 current，否则 outdated。
- 旧记录返回 `binding_status=legacy_unbound` 和警告；可读但不可作为新生成的事实输入。
- Rule/Explanation 创建前登记 temporary snapshot references，成功后转 permanent。

### 13.4 Repository Chat

删除 `database.search_features`、`list_events`、`stats` 等 Evolution 操作。允许操作限定为：

- 获取 View 中的仓库快照元数据；
- 查询 Snapshot 符号、调用者和调用树；
- 查询 Snapshot facts 和已绑定规则/解释；
- 查询已完成的 View Artifact。

Chat 请求必须携带 `view_id`，审计记录保存 view ID/digest、使用过的 Snapshot ID 和操作列表。
Chat 不主动创建快照或组合 Artifact；依赖结果不存在时返回明确的 not-generated 状态。

## 14. Graph View Artifact

### 14.1 类型归属

以下结果属于 View Artifact：

- topology；
- HTTP/MQ/gRPC/Redis flow；
- impact；
- 跨服务实体对齐；
- 前端调用者与后端 API 关联；
- resource dependency graph。

构建器接收 `ResolvedGraphView`，其中每个成员是 Snapshot Handle，禁止接收 registry path。

### 14.2 异步懒构建

首次 GET 未命中缓存时：

1. 校验 View 与参数；
2. 计算完整 `cache_key_digest`，先查与 View ID 解耦的 completed cache；
3. 未命中时复用该 key 的唯一活动 job，或创建 attempt_no 递增的新 job；
4. 返回 HTTP 202、job ID 和轮询位置；
5. 后台任务仅打开 View 中的 Snapshot Artifact；
6. 完成时先发布 immutable cache payload，再终结 job 并释放活动引用。

确定性 job 不触发 LLM。LLM entities 必须使用显式 POST 创建，并在 cache key 中包含 semantic
identity。服务重启时 pending/running Artifact job 标 interrupted；用户重新请求可创建新 job
或按 retry API 创建带 `retry_of_job_id` 的新 job。failed/interrupted/cancelled job 不占用活动唯一
索引，也不会阻止同 key 重试。completed cache 不引用具体 View；View 到期后仍可按 cache retention
独立回收，不会被 `ON DELETE RESTRICT` 卡住。

### 14.3 Payload 存储

- canonical JSON 小于 1 MiB：内联 `payload_json`；
- 大于等于 1 MiB：写内容寻址 Artifact，数据库保存 key、digest 和 byte size；
- API 对两种位置返回相同 JSON 资源契约；
- payload 永不因新的 current 或新 View 被原地覆盖。

## 15. HTTP API 契约

### 15.1 通用约定

- 时间：RFC3339 UTC；
- 分页：`cursor` + `limit`；
- 异步创建：202 + `Location`；
- 更新 metadata：`If-Match` 或 `metadata_version` 乐观锁；
- 错误：

```json
{
  "error": {
    "code": "snapshot_view_mismatch",
    "message": "The snapshot is not a member of this view.",
    "details": {},
    "request_id": "req-..."
  }
}
```

后台 Attempt 失败通过 Run 查询的 200 响应表达，不把 worker 失败映射为原请求的 5xx。

### 15.2 Catalog 与显式检查

```http
GET    /api/repos
POST   /api/scopes
PATCH  /api/scopes/{scope_id}
GET    /api/scopes/{scope_id}/members
POST   /api/scopes/{scope_id}/members
PATCH  /api/repository-members/{member_id}
DELETE /api/repository-members/{member_id}
POST   /api/repository-members/check
```

普通 GET 只读取数据库。member 的 `change_status` 是最后一次显式检查的缓存结果；从未检查为
`unknown`，不能现场计算或虚构 unchanged。

Scope rename 和 member rename/move 不改变稳定 ID。删除 member 只设置 `retired_at`；重新注册相同
路径不能自动继承旧 member ID，必须通过明确的 restore 操作恢复 retired member，避免错误拼接历史。

check body：

```json
{"member_ids":["member-a","member-b"]}
```

结果枚举：`unchanged | source_changed | analyzer_outdated | unparsed | check_failed`。检查只访问所选
成员，不 sync、不生成 Snapshot。`analyzer_outdated` 通过 Snapshot 版本身份与当前期望身份比较，
不需要访问现场；source 检查失败不覆盖之前成功的 current。若多个条件同时成立，主状态优先级为
`check_failed > unparsed > source_changed > analyzer_outdated > unchanged`；响应 details 同时返回
`source_changed` 和 `version_drift` 布尔值，避免主状态隐藏次要原因。

### 15.3 Analysis Run

```http
POST /api/analysis-runs
GET  /api/analysis-runs/{run_id}
POST /api/analysis-runs/{run_id}/retry
POST /api/analysis-runs/{run_id}/cancel
POST /api/analysis-runs/{run_id}/members/{member_id}/cancel
```

创建返回每个 member 的 `queued` 或 `already_running` disposition。重试请求只接受原 Run 中
failed/cancelled/interrupted 的 member，创建并返回新的 Run ID。取消幂等；终态重复取消返回当前
资源，不回滚结果。

Run 查询包括：counts、Attempt status/stage、progress、timestamps、snapshot、retry relation、
blocking attempt 和结构化错误。首期使用带 ETag/updated_at 的轮询，不引入 SSE。

### 15.4 Snapshot

```http
GET    /api/repository-members/{member_id}/snapshots?cursor=&limit=
GET    /api/repository-snapshots/{snapshot_id}?include=facts
PATCH  /api/repository-snapshots/{snapshot_id}/metadata
POST   /api/repository-snapshots/{snapshot_id}/deletion-preview
DELETE /api/repository-snapshots/{snapshot_id}
```

PATCH 仅允许 `label`、`note`、`pinned`，拒绝额外字段；pin 的引用变更与 metadata 更新同事务完成。
deletion-preview 返回引用、预计释放空间及有效 5 分钟的一次性确认 token，不改变数据。DELETE 必须
携带 `X-Deletion-Confirmation`；服务验证 token 后再次检查引用。成功接受删除任务并返回 202；受保护
返回 409 `snapshot_protected`，details 按 owner type/id 列出引用。已 trashed 资源返回 410。普通 API
不提供级联强删或绕过引用保护的 token。

### 15.5 Graph View 与 Artifact

```http
POST /api/graph-views/current
POST /api/graph-views
POST /api/graph-views/{view_id}/pin
GET  /api/graph-views/{view_id}
GET  /api/graph-views/{view_id}/export

GET  /api/graph-views/{view_id}/artifacts/{artifact_kind}
POST /api/graph-views/{view_id}/artifacts/entities/generate
GET  /api/graph-artifact-jobs/{job_id}
POST /api/graph-artifact-jobs/{job_id}/retry
POST /api/graph-artifact-jobs/{job_id}/cancel
```

Artifact GET 的参数规范化后生成 params digest。未生成返回 202 job；已完成返回 200。导出只包含
schema、view ID/digest、member→snapshot 映射和非敏感 provenance，不包含 registered path。只有
Pinned View 可导出；ephemeral View 返回 409 `view_not_pinned` 和 pin action，GET export 不隐式改状态。

### 15.6 知识和调用树

```http
GET  /api/knowledge?view_id=...[&member_id=...]
GET  /api/call-tree/children?view_id=...&repository_snapshot_id=...&node_id=...
GET  /api/call-tree/rule?view_id=...&repository_snapshot_id=...&node_id=...
POST /api/call-tree/rule/generate
POST /api/api-explanations/generate
POST /api/chat
```

省略 `member_id` 时返回按 View 聚合的报告，并为每个 unparsed/retired member 返回 availability
占位；提供 `member_id` 时只返回该成员投影，member 不属于 View 则返回 409。API facts 中的
`handler_node_id` 是调用树 root。缺少 View/Snapshot selector、Snapshot 不在 View、View 过期或
成员未解析都返回明确错误/availability，不允许 fallback。

### 15.7 主要错误码

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_request` | 参数组合错误 |
| 404 | `member_not_found` / `snapshot_not_found` / `view_not_found` | 资源不存在 |
| 409 | `snapshot_view_mismatch` | Snapshot 不属于 View |
| 409 | `snapshot_protected` | 删除存在引用 |
| 409 | `invalid_attempt_state` | 非法状态操作 |
| 410 | `view_expired` / `snapshot_gone` | 已过期或已 trash |
| 422 | `invalid_member_selection` / `retired_member` | 选择无效 |
| 424 | `snapshot_unavailable` | Artifact 依赖缺失/损坏 |
| 503 | `codegraph_cli_unavailable` | 解析环境不可用 |
| 507 | `insufficient_storage` | 空间预检失败 |

Attempt error code 至少覆盖 path/git、CodeGraph init/sync/timeout、source changed、integrity、capture、
unsafe symlink、storage、analysis、artifact publish、cancel、restart 和 internal error。所有 message 和
log_excerpt 必须限长并脱敏。

## 16. CLI 与 MCP

### 16.1 CLI

删除：

- `backfill`、`update`、旧 Evolution `status`；
- `init-all`；
- 任何隐式 live knowledge/topology/flow/entities 模式。

新增或调整：

```text
codeevolution analyze --member-id <id> [--member-id <id> ...]
codeevolution runs [--run-id <id>]
codeevolution runs retry --run-id <id> --member-id <id>
codeevolution runs cancel --run-id <id> [--member-id <id>]
codeevolution snapshots --member-id <id>
codeevolution views create (--scope-id <id> | --member-id <id> ...)
codeevolution views pin --view-id <id>
codeevolution knowledge (--snapshot-id <id> | --view-id <id>)
codeevolution topology --view-id <id>
codeevolution impact --view-id <id> --service <name>
codeevolution flow --view-id <id> ...
codeevolution entities --view-id <id> [--llm]
codeevolution check --member-id <id> ...
```

命令命名在实现时可按 argparse 一致性微调，但 selector 和 snapshot-only 语义不得改变。

### 16.2 MCP

删除 timeline/history/evolution summary 五类旧工具，替换为：

- list scopes/members/current snapshots；
- get Snapshot metadata/facts；
- search Snapshot symbols；
- get callers/callees/call tree；
- create/get/pin/export Graph View；
- 获取已完成的 View Artifact。

MCP 上下文持有 Snapshot application facade，不再持有 EvolutionStore。任何 MCP 工具都不能接受
仓库绝对路径作为查询输入。

## 17. 前端设计

### 17.1 Home

- 跨 scope/member checkbox、全选和清空；
- 顶部“解析已选择”“检查改动”“重试失败”操作；
- current Snapshot、last Attempt、last check 分开显示；
- 部分失败时成功 member 立即刷新 current，失败 member 同时展示旧 current 和本次错误；
- active Run ID 写 URL 或 localStorage，页面刷新后从 API 恢复，不依赖内存字典；
- 每行支持取消；Run 支持整体取消；
- 移除旧 `/api/repos/{name}/init[/status]` 和“初始化并回溯历史”文案。

进度阶段映射：等待、校验、摘要、初始化/同步、冻结图谱、冻结源码、分析、发布、完成。

### 17.2 Knowledge 页面

页面状态机：

```text
bootstrapping_view → loading_report → ready | incomplete | error
ready → creating_new_view → ready(new view) | ready(old view + refresh error)
```

进入页面时仅创建 current View，不解析；`view_id` 写入 URL query。顶部展示 view short digest、
created time、complete/incomplete 和每个成员的 Snapshot chip。后台 current 变化只提示有更新，
不自动切换。用户点击“刷新最新快照”时创建新 View，成功后原子替换页面状态并清空调用树等缓存。

`CallChainTree`、`NodeRuleRail`、Explanation 和 Chat props/request 全部贯穿：

```text
view_id + member_id + repository_snapshot_id + node_id
```

历史 Snapshot 浏览使用独立 View，不污染当前 scope View。

### 17.3 View 和 Snapshot 管理

- member drawer：Snapshot 历史、label、note、pin、删除保护说明；
- view actions：pin、export、查看成员映射；
- unparsed 成员显示占位和“前往解析”，不得隐藏；
- View 过期返回 410 时保留页面上下文并提示创建最新 View。

## 18. 保留、删除与磁盘管理

### 18.1 Retention

- 每成员保留最近 10 个成功 Snapshot（无论是否受保护），排序为 `captured_at DESC, id DESC`；
- 除上述最近 10 个外，current、snapshot pin、ephemeral/pinned View、Rule、Explanation、Artifact job
  引用的更老 Snapshot 也永不自动删除；
- failed Attempt 的日志和 observations 默认保留 30 天；
- retire member 不删除 Snapshot；Snapshot 标记 orphaned/成员 retired 可继续历史读取；
- retention 只创建 deletion job，不直接 unlink。

### 18.2 两阶段删除

1. 事务内重新检查 `snapshot_references`；存在有效引用则 409。
2. 标记 Snapshot `deletion_pending`，创建 deletion job。
3. worker 计算 Evidence/facts 是否仍被其他 Snapshot 引用。
4. 需要删除的 Artifact 原子移动到同文件系统 trash。
5. 事务中标记 trashed/移除可删元数据。
6. 安全宽限期后 unlink trash，完成 job。

任一步崩溃均可从 job 和 trash 恢复。物理清理失败不会恢复 Snapshot 为可读；可重试清理。
读路径遇到 deletion_pending 应拒绝新读，已有打开句柄可以完成当前操作。

### 18.3 空间预检

预估至少覆盖：CodeGraph backup、源码冻结、分析 staging、CAS publish 的峰值和配置安全余量。
创建 `storage_reservations` 防止两个并发 Attempt 同时通过预检后超卖空间。空间不足返回 507，
列出可清理类别与估算空间，但不得自动删除受保护结果。

### 18.4 Scavenger

服务启动后低优先级处理：

- 超过安全年龄且无 DB 引用的 staging；
- Artifact 已 rename 但 DB 无引用的 orphan；
- 到期 ephemeral View 和 temporary references；
- deletion jobs 和 trash；
- 过期 storage reservations。

Scavenger 不访问现场仓库，也不删除 current/pinned/referenced Artifact。

## 19. 故障与恢复矩阵

| 故障点 | 可见结果 | 恢复动作 | current |
|---|---|---|---|
| 创建 Run 后、claim 前崩溃 | Attempt interrupted | 用户重试新 Run | 不变 |
| CodeGraph init/sync 失败 | Attempt failed | 保留错误，显式重试 | 不变 |
| sync 中取消 | cancelled | 终止进程组，清 staging | 不变 |
| A/B/C 不一致或 E/F 不一致 | failed/source_changed | 清 staging，用户重试 | 不变 |
| SQLite backup/integrity 失败 | failed | 清 staging | 不变 |
| 分析失败 | failed | Evidence staging 可回收 | 不变 |
| fsync/rename 前崩溃 | interrupted | scavenger 清 staging | 不变 |
| rename 后、DB 事务前崩溃 | interrupted + orphan Artifact | scavenger 延迟清理 | 不变 |
| DB publish 事务回滚 | failed/interrupted | Artifact 按 orphan 回收 | 不变 |
| DB publish 提交后响应丢失 | completed 可查询 | idempotent GET/创建键确认 | 已更新 |
| 取消与 publish 竞争 | 先到线性化点者获胜 | 终态 CAS | 一致 |
| member 在运行中 retire | completed orphan Snapshot | 不发布 current | 不变 |
| View Artifact 中断 | job interrupted | 重新请求/重试 | 无影响 |
| 删除移入 trash 后崩溃 | deletion job 可恢复 | 继续 DB 标记和清理 | 目标已不可读 |
| Snapshot Artifact 损坏 | 424 snapshot_unavailable | 报警，不现场回退 | 指针保留供诊断 |

对 current 指向损坏 Artifact 的情况，系统不得自动切到其他快照；管理员/用户通过明确操作选择
历史 Snapshot 或重新解析，以避免静默改变查询语义。

## 20. 迁移与切换

### 20.1 Registry 迁移

1. 启动前读取旧 JSON registry；损坏时停止迁移并报告，不当作空列表覆盖。
2. 对 scope 和 member 生成稳定 UUID；重复 path、同 basename 和组内重名必须显式诊断。
3. 在新 SQLite 单事务导入，并写 migration marker 和原文件摘要。
4. 原 JSON 原子复制到 `migration-backups`；不删除源文件。
5. 后续所有注册、改名、迁移 scope 和 retire 只写 SQLite；JSON 不再是运行时数据源。
6. 重复启动根据 marker 和源摘要保持幂等，不生成新 member ID。

### 20.2 旧知识数据

- 旧 Business Rule、Node Rule、API Explanation 保留原记录并返回 `legacy_unbound`；
- 不根据 repo basename、路径或 endpoint 猜测 Snapshot 绑定；
- 新记录使用不可变 generation 与 snapshot reference；
- 旧 topology cache 不导入；第一个 View 按新 cache key 重建。

### 20.3 旧 Evolution Engine

一次性移除生产依赖和公开入口：HistoryWalker、EvolutionEngine、FeatureMatcher、
EvolutionAnalyzer、Evolution application services、Runtime Telemetry、旧 CLI、MCP、Chat operations、
Web init/backfill API 和相关 tests/benchmark。

保留用户磁盘的 `.codeevolution/evolution.db` 和 `.codehistory/evolution.db`。新启动流程不能探测、
打开、迁移或删除这些文件。旧 API 不代理到新 API，可返回 404；发行说明列出破坏性变更。

### 20.4 切换和回滚

每阶段通过 feature wiring 在内部完成，但正式切换时必须保证所有公开读取同时变为 snapshot-only，
不能长期运行“Knowledge 已快照化、Topology 仍 live”的混合模式。

代码版本回滚不删除新数据库或 Artifact。旧版本不能识别新 schema 时应拒绝写入；不得自动降级
schema。数据回滚依靠保留的 registry backup 和旧 evolution 文件，不把新 Snapshot 反向转换为
Evolution 数据。

## 21. 可观测性与审计

结构化日志至少包含 request/run/attempt/member/snapshot/view/artifact job ID、status、stage、duration、
错误码和 byte counts；不记录源码、secret、完整绝对路径或外部上下文内容。

建议指标：

- 活动/排队 Attempt 数和各 stage duration；
- completed/unchanged/failed/cancelled/interrupted 比例；
- CodeGraph init/sync duration；
- capture bytes、dedup bytes、空间 reservation；
- source_changed_during_capture 次数；
- View create/pin/expire 数；
- Artifact cache hit/miss 和构建时间；
- retention、trash、orphan 和 GC 失败数。

审计事件记录注册变更、Run 创建/取消/重试、Snapshot 发布/metadata/删除、View pin/export、规则与
解释生成。`external_context` 和 tags 原样保存但尺寸受限，展示和日志默认不展开。View export 默认
不包含 external context；只有显式请求并通过本地权限检查时才包含。

## 22. 安全模型

威胁边界包括不可信仓库内容、路径、symlink、超大文件、恶意 SQLite、prompt injection 和日志泄密。

- 所有 artifact 相对路径在打开前重新规范化并校验 manifest membership；
- SQLite 以只读 URI 打开冻结副本，执行 schema allowlist/integrity 验证；
- SourceProvider 只允许读取 manifest included 文件；
- 分析器不得自行 `open/rglob` 注册目录；
- secret 排除先于任何分析读取；
- report、errors、logs 和 export 做路径/token 脱敏；
- LLM prompt 只读取显式允许的 Snapshot evidence，并遵守现有 source chunking 限制；
- external context 必须是 JSON object，不解释其值，不进入 shell、路径或 digest；
- CodeGraph 命令参数使用 argv，不拼接 shell 字符串。

## 23. 实施拆分与依赖顺序

### Phase 1：持久化核心与手动解析

1. 新数据目录、schema migration、SQLite catalog；
2. registry 一次性迁移；
3. Artifact Store、scanner、CodeGraph command/backup；
4. Run/Attempt scheduler、A/B/C 校验、publish；
5. Snapshot history/current/check API；
6. Home 选择、进度、取消、重试 UI。

阶段出口：可以对任意 member 集合手动解析，部分成功独立发布，服务重启可恢复状态。

### Phase 2：所有单仓读取快照化

1. Snapshot Handle、SourceInventory 和现有分析器端口化；
2. Knowledge、Call Tree、Node Rule；
3. Explanation、Business Rule immutable generation；
4. Chat snapshot operations；
5. CLI 单仓查询与 MCP Snapshot 工具。

阶段出口：删除/改名现场仓库后，历史 Snapshot 的全部单仓读取仍工作；无任何 live fallback。

### Phase 3：Graph View 和组合事实

1. ephemeral/pinned View、TTL、references；
2. Topology/Flow/Impact/Entities/Frontend callers 输入端口化；
3. Artifact job、cache key、内联/CAS payload；
4. Knowledge 页 View 固定、refresh、pin/export；
5. 跨仓 CLI/MCP。

阶段出口：旧页面 View 在新 Snapshot 发布后保持一致，所有组合事实严格绑定 digest。

### Phase 4：生命周期与旧系统移除

1. retention、storage reservation、two-phase deletion、scavenger；
2. 完整移除 Evolution/Runtime Telemetry 和旧入口；
3. 更新 README、AGENTS、design、帮助文本和发行说明；
4. 删除 live topology cache 和迁移期兼容 wiring。

阶段出口：生产依赖图中不存在旧 Evolution 或 live repository knowledge read。

## 24. 测试策略

### 24.1 No-live-fallback 契约测试

生成 Snapshot 后删除、移动或禁止访问现场仓库；Knowledge、Call Tree、Node Rule、Explanation、
Chat、Topology、Flow、Entities、Frontend association 必须继续工作。无 Snapshot 时返回 unparsed，
不得访问注册路径。

### 24.2 一致性与并发

- WAL 模式下 backup integrity；
- sync、复制和冻结后分别增删改文件；
- symlink swap；
- 同 member 两个并发 Run 只有一个活动 Attempt；
- A/B/C 必须相等，期望冻结集合 E 与副本集合 F 必须相等，否则不发布；
- remove during run 只产生 orphan Snapshot；
- cancel/publish 竞态按线性化点得到唯一终态；
- 两个空间预检通过请求不会超卖 reservation。

### 24.3 Digest 与确定性

- 文件枚举和 JSON key 顺序不影响摘要；
- capture time、branch、绝对路径不影响内容身份；
- SQLite 物理布局变化但逻辑 rows 相同时 logical digest 相同；
- 源码、规则、options、schema、算法版本变化分别失效正确层；
- 同输入重复分析产生 byte-identical canonical facts。

### 24.4 View 与缓存

- V1 创建后发布 member V2，V1 仍读取旧 Snapshot；
- refresh 得到新 View；
- 新增 unparsed member 改变 digest并标 incomplete；
- View TTL 滑动、过期、pin 和 export；
- Artifact params 不串缓存；
- deterministic 和 LLM entities 缓存隔离；
- 跨服务调用树始终使用目标 Snapshot ID。

### 24.5 生命周期与故障注入

- 每个 fsync、rename、DB begin/commit 前后崩溃；
- staging、orphan、trash 恢复；
- current/pin/View/Rule/Explanation 各类引用均返回 409；
- 共享 Evidence 只在最后引用消失后回收；
- 最近 10 个成功 Snapshot 和 30 天日志边界；
- Artifact 损坏只报错，不现场回退或自动切 current。

### 24.6 安全

- `.env*`、key、cert、ignored、submodule、外部 symlink、binary、oversize；
- path traversal、NUL、case/Unicode 路径碰撞；
- 目录和文件权限；
- 日志、错误、manifest 和 export 不泄露 secret/绝对路径；
- 恶意 client 不能用任意 snapshot_id 绕过 View membership。

### 24.7 迁移与公开契约

- registry migration 幂等，rename/move 不改变 member ID；
- 损坏 registry 不被空数据覆盖；
- legacy rule/explanation 明确 unbound；
- 旧 topology cache 不复用；
- CLI/MCP/Web 不再公开 Evolution 工具；
- 启动和迁移永不修改已有 evolution.db。

## 25. 验收追踪

| 上游验收主题 | 技术控制点 | 核心测试 |
|---|---|---|
| 手动与隔离 | Snapshot-only ports、显式 check/analyze | no-live-fallback |
| 仓库独立性 | member Attempt、独立 publish transaction | partial success |
| 一致性 | A/B/C + E/F、SQLite backup、View binding | mutation/failure injection |
| 可靠性 | partial unique index、CAS、restart recovery | race/crash tests |
| 生命周期 | references、retention、two-phase delete | protection/GC tests |
| 旧能力移除 | dependency/import/API/CLI/MCP 清单 | public contract tests |

## 26. 方案完整性检查（Grill with Document）

本设计按“决策树逐层展开”的方式完成方案审查。以下问题已发现并闭合：

| 检查维度 | 原方案空白或冲突 | 已确认结论 |
|---|---|---|
| 执行边界 | 是否需要外部 worker | 单进程持久调度器 + CodeGraph 子进程组 |
| View 可恢复性 | 普通 View 不落库却只传 view_id | 24h 滑动 TTL 短期落库 |
| Registry | JSON 与 SQLite 双真相 | SQLite 是唯一运行时 catalog |
| Retry | 原 Run 重开语义不清 | 新 Run + retry_of_attempt_id |
| View scope | 全局成员与 scope 页面冲突 | selector 解析为不可变成员集合 |
| Snapshot 内容 | report 截断导致历史事实丢失 | 保存完整 canonical facts，读取时投影 |
| Graph identity | SQLite bytes 不等于图语义 | physical + logical 两类 digest |
| Secret | 现有分析器读取 `.env` | 永不读取/冻结，降级并标 incomplete |
| 组合计算 | 同步还是异步 | 持久异步 job、按完整 key 懒构建 |
| Cancel race | publish 后仍可能 cancelled | publish 前最后检查，事务开始后发布获胜 |
| TOCTOU | 两次摘要不足 | A/B/C 现场一致 + E/F 冻结一致 |
| Delete/GC | DB 与文件无法同事务 | deletion job + trash 两阶段恢复 |
| Reference | 多数据库扫描易漏 | 中央 snapshot_references 权威索引 |
| CLI/MCP | 仍可能 live read | 强制 Snapshot/View selector |
| 大 payload | SQLite/WAL 膨胀 | 1 MiB 阈值，超限进入 CAS |

同时通过代码审查确认了 Knowledge、Grouped frontend calls、Call Tree、Node Rule、Topology、
Flow、Explanation freshness、Chat、CLI 和旧 init task 的全部现场依赖入口。本文已为每类入口定义
目标端口、迁移归属和验收测试。

### 26.1 明确延后但已留扩展点

以下事项不属于本期，且不会阻塞本文实施：

- 两个 Repository Snapshot 之间的差异分析；
- 多服务实例调度和 lease/fencing；
- 团队 release/部署环境/兼容 commit 模型；
- 自动后台解析或文件监听；
- Git 历史回溯；
- 普通查询自动触发 LLM。

### 26.2 实施中不得重新打开的决策

除非先修订上游方案和本文，否则实施任务不得：

- 为方便兼容而加入 live fallback；
- 将旧 `/init`、Evolution CLI 或 MCP 工具映射到新语义；
- 把普通 View 改回纯内存对象；
- 将 `.env` 内容纳入分析或 Artifact；
- 用 SQLite 文件哈希代替 logical graph digest；
- 在 Snapshot 内容表中原地更新分析结果；
- 取消中央引用检查或两阶段删除；
- 将单仓 facts 与多仓 View Artifact 混存为同一事实层。

至此，产品行为、领域身份、执行状态、持久化、Artifact、API、UI、迁移、安全、恢复和验收各分支
均已有明确落点，方案完整性检查闭合。
