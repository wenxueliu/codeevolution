# CodeEvolution Web 控制台 UI/UX 设计

> 版本：v1.0
> 状态：与 PRD v0.2 对齐的页面级设计
> 更新日期：2026-10-14
> PRD：[`prd.md`](prd.md)
> 技术栈：Vue 3（Options API）· vue-router 4（hash 模式）· Vite · Element Plus（部分使用）· mermaid
> 国际化约定：见 [AGENTS.md「前端国际化约定」](../AGENTS.md)

## 1. 设计范围与 PRD 需求映射

本文将 PRD 的功能需求映射到具体页面、组件和交互。产品是本地优先工作台，没有账号/权限体系，按钮的可见性由**上下文状态**（是否选择 Snapshot/View、LLM 是否配置、任务是否在跑）决定，而不是角色。

| PRD 需求 | 页面/组件 | 本文章节 |
|---|---|---|
| FR-01 注册 Scope/成员 | Home | §4 |
| FR-02/03/11 分析 Run、快照、现场检查 | Snapshots | §5 |
| FR-04/08 知识中心、语义解释（批量/提示词/状态） | Knowledge | §6 |
| FR-06/07 Graph View、拓扑、Impact/Flow | GraphView | §7 |
| FR-13/14 术语识别、审核与跨服务对齐 | Terms | §8 |
| FR-09/10 问答、审计、UI 测试；LLM 设置 | 全局组件 | §9 |
| FR-12 快照保留/删除 | Snapshots / API | §5 |
| FR-15 i18n、异步任务、状态展示、可访问性 | 全局横切面 | §10 |

## 2. 角色与任务映射

PRD §4 的五种角色是任务视角，不是权限角色。所有角色看到同一界面：

| 任务 | 主要入口 |
|---|---|
| 接入/管理代码仓 | Home |
| 发起分析、监控 Run、建 View | Snapshots |
| 浏览结构知识、调用链、语义解释 | Knowledge |
| 跨仓拓扑、影响与流程 | GraphView |
| 统一业务语汇、审核术语 | Terms |
| 问答与审计 | RepositoryAssistant（全局悬浮） |
| 配置 LLM | LLMSettings（全局弹层） |

## 3. 全局框架

### 3.1 导航与布局（`App.vue`）

- 顶栏：品牌（CodeEvolution）· 知识中心 · Snapshots · Graph View · 当前仓库名（进入过知识页时）· "LLM 设置"按钮 · 语言切换按钮（`EN`/`中`）。
- 底部全局挂载 `<RepositoryAssistant>`（悬浮问答侧栏）与 `<LLMSettings>` 弹层。
- 全局请求错误以页面顶部红条（`request-error`）展示，可关闭。
- 路由为 hash 模式；Knowledge 支持 hash query 携带 `snapshot_id`。

### 3.2 导航上下文持久化（`navigationContext.js`）

sessionStorage 保存 `codeevolution:last-snapshot-id` / `-member` / `-graph-view-id`，并以自定义事件 `codeevolution:navigation-context` 通知组件；Snapshots 另存最近 Run ID（`LAST_RUN_KEY`）。跨页跳转（Snapshots → Knowledge → 调用链）必须恢复上下文，不允许用户重新选择。

## 4. Home（`/`，代码仓接入）

布局：按 Scope 分组的仓库卡片列表 + "添加代码仓"表单。

| 功能 | 交互 |
|---|---|
| 添加代码仓 | 名称 + 绝对路径；只保存注册记录，不修改目标仓库（FR-01） |
| Scope 成员 | 卡片内联添加/移除成员（`add-member` / `member-remove`）；移除仅删除注册记录并有确认提示 |
| 进入分析 | 每卡片"进入 Snapshots"按钮 |
| 删除注册 | confirm 后删除；提示不删除实际代码与历史 Snapshot |

空/加载/错误态统一走 UiState（§10.2）；空态文案给出下一步动作（先去注册仓 → 再去 Snapshots）。

## 5. Snapshots（`/snapshots`，分析控制台）

- **Run 卡片**（`data-testid: analysis-run`）：状态徽章 `.status-{pending|queued|running|completed|partial|failed|cancelled|unchanged}`、进度条、取消 Run、失败成员数与"重试失败成员"按钮。活跃 Run 用 `setInterval` 轮询，`beforeUnmount` 清理。
- **成员选择表**：全选 checkbox（支持 indeterminate）、成员级选择（跨会话记忆选择）、"已发布/未解析"状态、"检查改动"（`change-status`，FR-11 四态：unchanged / source_changed / unparsed / check_failed）、"查看知识"链接。
- **创建当前 View / 查看跨仓图谱**：以选中成员的 current Snapshot 组合创建（FR-06）。
- 部分成功（Run `partial`）时，已发布成员立即可用，不等待全部成员完成。
- 快照删除/保留：Web 端暂以确认弹窗与删除预览 API 支撑（FR-12），入口持续完善中。

## 6. Knowledge（`/repo/:repoName`，知识中心，最重页面）

两种形态：无 `repoName` 为**项目目录态**（项目卡片 + 引导去 Snapshots）；有 `repoName` 为**知识态**。页面标题显示所选服务名与 Snapshot ID。

### 6.1 概要与分区

- 概要卡：API 端点 · 术语 · 模块 · 核心实体 · 测试覆盖率 · 分层违规。
- 14 个知识分区（`SECTIONS` 常量）按阶段分组：阶段 1/2 结构知识（api_contract、terms、module_topology、core_entities、test_coverage、layer_violations、config_consumption、external_dependencies、authorization_model、heat_map）默认可用；阶段 3 LLM 分区（business_descriptions、business_rules、error_catalog、state_machines）显式提示配置与费用。
- LLM 抽取按钮 + `llm-progress` 进度条轮询 job 状态；失败不影响其他分区浏览。

### 6.2 API 解释（FR-08 重点）

- **设置面板**（`api-explanation-settings`）：系统默认模板可展开对照；local / synthesis / aggregate 三个模板编辑；保存 / 保存并应用到全部端点；历史提示词版本折叠展示。
- **批量任务卡**（`api-explanation-batch`）：进度条、完成/失败/跳过/取消计数、取消任务（confirm，取消后状态可见）、失败重试、端点明细折叠；轮询 batch 状态。
- **端点表**：路径搜索、HTTP 方法筛选、服务筛选、分页；行展开证据区：CallChainTree + NodeRuleRail + 源码面板、mermaid 时序图（放大弹窗 `sequence-dialog`，± 缩放）、生成解释、解释快照管理（列表 + 删除）、端点级自定义提示词弹窗（`endpoint-prompt-dialog`）、业务规则区（生成/重试/编辑）。
- 每端点解释状态徽章 `.explanation-{status}`（pending/running/validating/completed/partial/failed/cancelled），来自 `/api/api-explanations/statuses`。

### 6.3 术语分区

按可信度排序的术语表，状态徽章 `.term-status.{accepted|needs_review|rejected}`，证据数列；与 Terms 页共享同一套状态语义（§8）。

## 7. GraphView（`/graph-views/:viewId?`，跨仓工作台）

- **目录态**：已固定/临时 View 列表（el-card），显示快照完整性（含不可用成员时警示）、成员数 tooltip、"查看 View"；空态引导去 Snapshots 创建。
- **详情态**：
  - 概要（el-descriptions）：View ID、成员、digest。
  - 未生成 artifact 时展示 el-result + "生成拓扑"按钮 + artifact job 轮询；不因打开页面隐式创建任务。
  - 服务拓扑 SVG（TopologyGraph：rect 节点 + 箭头边，按 edge-kind/message/grpc/resource 配色）。
  - 服务边表：调用方 → 目标、类型、置信度、证据（el-collapse 手风琴）；候选/歧义/Scope 外边界/资源依赖单独呈现。
  - 影响与流程：起始 API 表单 → 下游/上游 el-tag、静态流程图。
- 拓扑/Impact/Flow 全部绑定同一 `view_id`（PRD 不变量 2）。

## 8. Terms（`/terms`，术语识别与审核）

### 8.1 跨服务术语对齐（FR-14，上半区）

- 依赖已选 Graph View；展示推荐服务对（技术服务被排除并显示原因）、对齐候选表（同名 `same` / 相关 `related`）。
- "生成对齐候选"按钮显式触发；候选状态 `needs_review`，人工 accept/reject 后生效。

### 8.2 单仓术语识别（FR-13，下半区）

- 工具栏：状态筛选 / 类型筛选（resource、entity、value_object、state、event、action、technical）/ 关键词搜索（防抖）、"重新识别"（显式调用 `/api/terms/extract`）、人工补充表单（规范名 / 类型 / 定义 / 作者）。
- 术语表按可信度降序：排名 · 术语名（+ 别名）· 类型 · 可信度（% + 档位）· 状态 · 证据数。
- 行点击展开证据链行：证据类型（如 `api_route`、`entity_definition`）、证据值、`file:line`。
- 行级操作只暴露 接纳/排除/恢复（rejected → 恢复）；重命名/重分类/补别名走 API。
- **术语准确性约定**：数据操作名（Get/Add/Query/Create 等前缀）不出现在列表中；领域动作（如 ApproveOrder）保留并通过 `applies_to` 关系指向所操作的名词；DTO 包装名（如 CreateUserRequest）以别名归并进真实术语。

## 9. 全局组件

| 组件 | 用途 | 关键交互 |
|---|---|---|
| `UiState.vue` | 统一空/加载/错误态 | kind=empty/loading/error；error 态 `role=alert`，带重试/关闭 |
| `RepositoryAssistant.vue` | 全局悬浮问答侧栏（有仓库上下文时） | 三 tab：问答（受控查询计划可展开，不暴露任意 SQL）、审计日志、UI 测试录制（目标表单 / 录制中状态 / 同步步骤 / 停止，轮询 activeRecording） |
| `LLMSettings.vue` | LLM 配置弹层 | base URL / key / model；"测试连接"；key 不回显 |
| `CallChainTree.vue` | 调用链树 | el-tree 懒加载（函数/跨服务/external/回环节点）；右侧上下分栏：NodeRuleRail + NodeSourcePanel，可拖拽分隔条 |
| `NodeRuleRail.vue` | 节点解释与聚合右栏 | 节点/聚合解释、业务规则生成、提示词编辑、手动刷新、分页 |
| `NodeSourcePanel.vue` | 源码片段面板 | file/line 缺失时提示不可用 |
| `TopologyGraph.vue` | SVG 拓扑图 | 空态 el-empty |

## 10. 全局横切面

### 10.1 国际化（FR-15）

- 自研轻量方案：`i18n.js` 的 `t(key, params)`，`zh`（默认）/`en` 两个词典，key 为中文原句。
- 语言选择存 localStorage `codeevolution:locale`；切换广播 `codeevolution:locale-change` 事件；`document.documentElement.lang` 同步。
- 占位符用 `{name}`/`{count}` 风格，禁止字符串拼接绕过翻译；仓库名、路径、符号名、模型名、错误详情等业务数据原样展示。
- 切换语言只重新渲染文案，保持路由、Snapshot/View 上下文与进行中任务不变。

### 10.2 状态展示约定

- 页面级三态（加载/空/错误）统一 UiState；业务状态徽章为纯 CSS 类：
  - Snapshots：`.status-{status}`（Run/Attempt 状态文字色方案）
  - Knowledge：`.explanation-{status}`、`.term-status.{...}`
  - GraphView：el-tag type（success/warning/info）
- 状态码/阶段码由前端映射为当前语言文案；业务数据（仓库名、符号名、错误详情）不进入词条。

### 10.3 异步任务与轮询

| 任务 | 展示位置 | 轮询方式 |
|---|---|---|
| Analysis Run / 成员 Attempt | Snapshots Run 卡片 + 成员表 | setInterval，离开页面清理 |
| 拓扑 Artifact Job | GraphView | setInterval |
| LLM 知识抽取 job | Knowledge 概要区 | setTimeout 轮询 |
| API 解释批量任务 | Knowledge 批量任务卡 | 轮询 batch 状态 |
| API 解释单条/快照 | 端点行徽章 + 解释快照列表 | 状态接口刷新 |
| UI 录制会话 | RepositoryAssistant 录制 tab | 轮询 activeRecording |

所有长任务必须显式触发（按钮），可取消、可见失败原因；页面读取不产生新任务。

### 10.4 视觉与主题

- 单一亮色主题；品牌红 `#e94560`，导航深底 `#1a1a2e`；无暗色模式。
- CSS 组织：App.vue 全局样式 + 每页面/组件 `<style scoped>`，无预处理器与 design tokens。
- 组件双风格并存：Element Plus 集中在 GraphView 与 el-tree；其余页面用原生 table/button（`.primary/.secondary/.sm`）与自绘 backdrop 弹窗。新增界面优先沿用所在页面的既有风格。

### 10.5 可访问性与响应式

- 基础覆盖：focus-visible outline、aria-label、`role=dialog/alert`、键盘 Enter 展开。
- 响应式仅一个断点（`max-width: 900px`，顶栏折叠）；主要面向桌面工作台。

## 11. 测试与验收对照

| 验收面 | 依据 | 覆盖 |
|---|---|---|
| i18n：默认中文/切换/持久化/无混排 | AGENTS.md 国际化验收 | `web/tests/i18n.spec.js` |
| PRD 页面旅程（UI-001~UI-018） | PRD §8 | `web/tests/prd-ui-acceptance.spec.js` |
| 页面集成（导航上下文、术语审核 reject/restore、LLM 脱敏、取消/重试等） | 本文各节 | `web/tests/pages.integration.spec.js`（35 例，data-testid 断言） |
| 调用链解释右栏 | §6.2 | `web/tests/api-explanation-call-chain.integration.spec.js` |
| 视觉基线 | `tests/e2e/baselines/screenshots/` | 人工比对图片；暂无自动化视觉回归 runner |

新增/修改页面时的最低验收：中文默认、英文切换与持久化、核心页面无混合文案、动态状态与空态/错误态双语、进行中任务不因切换中断。
