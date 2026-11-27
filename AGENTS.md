# AGENTS.md

> **CodeEvolution** — 代码仓功能演进分析 + 业务知识逆向系统。

本文件是本服务唯一的项目文档（合并自原 `CLAUDE.md` 与 `AGENTS.md`，去掉了二者互相 `@` 引用造成的循环）。`CLAUDE.md` 仅作入口指向本文件；Claude Code 会自动加载同级的 `CLAUDE.md` 与 `AGENTS.md`，因此只需编辑本文件。

## 项目概述

CodeEvolution 三大子系统：

1. **Evolution Engine** — 分析 git 历史，以「功能」（入口点 + 调用树）为单位追踪代码演进
2. **Knowledge Extractor** — 从代码逆向业务知识（三阶段 13 维），服务产品/架构/开发/测试/运维
3. **Cross-Repo Analyzer** — 多仓微服务统一拓扑：跨服务调用拼接 + 影响分析 + 流程追踪 + 实体对齐

## 工作原则

- **尽量使用 subagent**：复杂/多步骤/跨文件搜索或分析任务，优先通过 Agent 工具启动 subagent 并行处理。
- **先读设计文档**：修改代码前先理解 `docs/design.md` 中的架构决策。
- **改完代码自动打包重启**：后端/前端/依赖变更后按下方「自动打包与重启」执行。

## 技术栈

- Python 3.10+ / SQLite (WAL) / FastMCP / networkx / openai（可选）
- 前端 Vue 3 + Vite（`web/`），后端 FastAPI（`codeevolution/api.py`）
- **代码解析完全委托 CodeGraph**（`@colbymchenry/codegraph` v0.9.x）：直接读目标仓库 `.codegraph/codegraph.db`（SQLite）取图谱，无需 CodeGraph 服务进程；使用前需在目标仓库先跑 `codegraph init`；覆盖 30+ 语言。

## 常用命令

```bash
# 前置：为目标仓库建立代码图谱
npm i -g @colbymchenry/codegraph@0.9.x && cd <repo> && codegraph init

# 快照分析：先通过 Web API 创建 analysis run，再对已发布快照查询知识
codeevolution knowledge --snapshot-id <snapshot-id> [-s api]

# ADR：从提交/文件/文本/历史生成草稿，查看调试映射，导出到仓库 docs/adr
codeevolution adr gen --snapshot-id <id> --commit HEAD
codeevolution adr gen --snapshot-id <id> --text "网关从 Node 迁移到 Rust"
codeevolution adr gen --snapshot-id <id> --file app/payments.py
codeevolution adr gen --snapshot-id <id> --history --limit 20
codeevolution adr gen --snapshot-id <id> --commits <sha1>,<sha2>
codeevolution adr commits --snapshot-id <id> --scan --limit 50
codeevolution adr template show --snapshot-id <id>
codeevolution adr backtest <draft-id>
codeevolution adr review <draft-id> --approve --rating 4 --comment "同意" --reviewer alice
codeevolution adr edit <draft-id> --file refined.md --editor alice --note "精简措辞"
codeevolution adr list --snapshot-id <id>
codeevolution adr show <draft-id>
codeevolution adr debug <generation-id>
codeevolution adr export <draft-id>
codeevolution adr prompts set --snapshot-id <id> --kind commit --file prompt.md

# 分层规则：从现有代码图反推声明，再按声明检查（支持基线，存量渐进收紧）
codeevolution layers init --db .codegraph/codegraph.db
codeevolution layers check --db .codegraph/codegraph.db
codeevolution layers check --db .codegraph/codegraph.db --write-baseline

# 多仓分析（先 register 注册服务）
codeevolution register -n <name> -r <repo>
codeevolution topology --view-id <view-id>
codeevolution impact --view-id <view-id> --service <member-id>
codeevolution flow --view-id <view-id> --service <member-id> --entry-id <entry-id>

# Web 控制台
codeevolution web                    # http://0.0.0.0:8765
```

### 前端国际化约定

- Web 控制台支持简体中文（`zh`）和英文（`en`），默认使用中文；语言选择保存在浏览器 `localStorage`，可通过顶部导航的语言按钮切换。
- Snapshots、Graph View、知识中心及其调用链/解释侧栏的用户可见文案必须经过 `web/src/i18n.js` 的 `t()` 翻译，不得直接新增中英文混排的硬编码文案。
- 新增页面文案时，同时补充 `zh` 与 `en` 词条；动态数量、名称和状态使用 `{count}`、`{name}` 等占位符，不能通过字符串拼接绕过翻译。
- API 返回的仓库名、路径、符号名、模型名、错误详情和知识内容属于业务数据，原样展示，不进入界面词条；状态码/阶段码由前端统一映射为当前语言。
- 语言切换必须保持路由、Snapshot/View 上下文和进行中的异步任务不变；切换后只重新渲染文案，不重复触发数据请求。
- 国际化验收至少覆盖：默认中文、英文切换与持久化、Snapshots/Graph View/知识中心无混合 UI 文案、动态状态和无数据/错误状态双语显示。

国际化实施方案：第一阶段采用轻量级本地词典和统一 `t(key, params)` 接口，覆盖当前 Web 控制台核心页面；第二阶段将词典按页面拆分并加入缺失 key 检查、英文伪本地化和视觉回归；第三阶段再评估引入成熟 ICU messageformat 能力，以支持复数、日期和更复杂的区域格式化。后端 API 保持语言无关，必要时由请求头或用户设置扩展服务端生成内容的语言偏好。

完整命令以 `codeevolution --help` 与 `codeevolution/cli.py` 为准；旧的
`backfill`、`update`、演进 `status` 和 `init-all` 已移除。

## 能力矩阵（18 维 × 5 角色，兼作命令速查）

| # | 能力 | 产品 | 架构 | 开发 | 测试 | 运维 | 命令 |
|---|------|:---:|:---:|:---:|:---:|:---:|------|
| 1 | API 契约 | x | x | x | x | | `knowledge --snapshot-id <id> -s api` |
| 2 | ADR 生成与导出 | x | x | x | | | `adr gen --commit HEAD` / `adr export <draft-id>` |
| 3 | 模块拓扑 | | x | x | | | `knowledge --snapshot-id <id> -s modules` |
| 4 | 核心实体 (PageRank) | | x | x | | | `knowledge --snapshot-id <id> -s entities` |
| 5 | 分层违规 | | x | | | | `knowledge --snapshot-id <id> -s layers` |
| 6 | 配置消费图 | | | | | x | `knowledge --snapshot-id <id> -s config` |
| 7 | 外部依赖清单 | | x | | | x | `knowledge --snapshot-id <id> -s deps` |
| 8 | 权限模型 | | x | | | x | `knowledge --snapshot-id <id> -s auth` |
| 9 | 热力图 | | x | x | | | `knowledge --snapshot-id <id> -s heatmap` |
| 10 | 业务描述 | x | | x | | | `knowledge --snapshot-id <id> -s business --llm` |
| 11 | 业务规则 | x | | | x | | `knowledge --snapshot-id <id> -s rules --llm` |
| 12 | 错误目录 | | | | x | x | `knowledge --snapshot-id <id> -s errors --llm` |
| 13 | 状态机 | x | | | x | | `knowledge --snapshot-id <id> -s states --llm` |
| 14 | 统一服务拓扑 | | x | x | | x | `topology --view-id <id>` |
| 15 | 跨仓变更影响 | | x | x | x | | `impact --view-id <id> --service <id>` |
| 16 | 全通道流程追踪 | | x | x | x | x | `flow --view-id <id> --service <id>` |
| 17 | 跨服务实体对齐 | | x | x | | | Web/API Graph View |
| 18 | 服务发现+健康检查 | | | | | x | `discover` / `check` |

ADR 的核心是**用模型生成**，不是读取已有文档：

- **提交表格**：`adr commits` **分页覆盖整个 git 历史**（`--offset/--limit`，默认每页 50、
  上限 100），总数与 `git rev-list --count HEAD` 一致；**合并提交保留在表中并直接标为已跳过**
  （不送 AI 分诊、不可勾选生成，`--no-merges` 可隐藏），随后规则过滤文档/测试/lockfile
  噪声（也标为已跳过），再由「提交分诊」提示词按**当前页**判断 bug fix、lint、test-only 等；
  结果按 `(member, sha)` 缓存，提交行与草稿/生成记录按每条提交的最新记录全量映射（不再截断）。
  每行展示生命周期状态：待生成（pending）→ 排队中（queued）→ 生成中（generating）→
  待校验（generated）→ 待审批（backtested）→ 已通过/已驳回（approved/rejected）→
  已导出（exported），另有已跳过（skipped）与失败（failed）；状态计数与已处理进度始终按
  **整段历史**统计（不受翻页/过滤影响），并支持按状态过滤（`--stage` / `?stage=`）与
  日期范围（`--since/--until`）。历史超过 20000 个提交时要求先缩小日期范围，避免给出
  只覆盖部分历史的进度。支持单个生成、勾选批量生成（`adr gen --commits`）。
- **生成**：`adr gen` 支持 `--commit`、`--file`、`--text`、`--history`、`--commits`；
  提示词按「服务 × 来源类型」版本化（commit/file/text/triage/backtest 五类）。
- **输出模板**：默认 MADR 4.0，可按服务覆盖；占位符包括 `{{frontmatter}}`、`{{title}}`、
  `{{context}}`、`{{drivers}}`、`{{options}}`、`{{outcome}}`、`{{consequences}}`、
  `{{confirmation}}`、`{{options_detail}}`、`{{evidence}}`，`{{title}}` 必填。
- **回测**：`adr backtest <draft-id>` 把 ADR 与当前代码证据（evidence 文件、仓库文件清单、
  最近提交）交给「回测」提示词，输出 verdict（consistent/partial/contradicted/
  unverifiable）、confidence、理由与证据。
- **人工审批**：`adr review <draft-id> --approve|--reject [--rating 1-5] [--comment]
  [--reviewer]`；审批后才允许 `adr export` 写入仓库 `docs/adr` 并重建索引（未审批返回 409）。
- **精修与版本**：`adr edit <draft-id> --file refined.md [--title] [--editor] [--note]`
  （`PUT /api/adr/drafts/{id}`）把改写后的 MADR 存为新版本：`revision + 1`、审批重置为
  pending、旧回测失效；已导出草稿不可再编辑或重新审批（409）。
- **交付物溯源**：导出时在 frontmatter 注入 `provenance:`（draft/generation、model、
  prompt_digest、evidence、editor、reviewer、exported_at），重复导出只替换该块。
- **调试映射**：每次生成/回测都保存来源内容、实际渲染提示词、模型原始响应、解析结果与
  耗时；`adr debug <generation-id>` 与 `GET /api/adr/generations/{id}` 可直接查看。
- **编码与 Windows**：导出的 ADR 与 `index.md` 以 UTF-8（带 BOM）写入，Windows 记事本、
  资源管理器预览等可直接识别而不再乱码；读取侧同时兼容 BOM、UTF-16 与 GB18030，
  仓库中存量 GBK 文档、GBK 提交信息和非 UTF-8 源文件会按原语言解码，而不是变成 `�`
  （统一入口 `codeevolution/text_encoding.py`）。已生成的历史草稿不会自动重编码，
  需要重新生成才能覆盖来源中的替换字符。

所有状态机数据落在 `~/.codeevolution/adr.db`（`adr_prompts` / `adr_generations` /
`adr_drafts` / `adr_draft_revisions` / `adr_templates` / `adr_commit_scans` /
`adr_backtests`）。分析发布时仍会读取
仓库已有 ADR 进入 Snapshot facts（`-s adr`）并执行 R1/R2 结构校验；旧 Snapshot 返回
`available: false` 而不是报错。

## 自动打包与重启

修改 codeevolution 代码后，Agent 自动执行打包和重启。

### 触发条件

以下变更应触发自动打包+重启：

- **后端代码变更**: `codeevolution/*.py` 任意 Python 源文件
- **前端代码变更**: `web/src/**` 任意 Vue/JS/CSS 文件
- **依赖变更**: `pyproject.toml` 或 `web/package.json`

纯文档变更（`*.md`）和 CI 配置（`.github/`）不需要打包重启。

### 自动打包

```bash
# 在服务根目录执行
cd services/codehistory

# 构建前端（npm ci + npm run build → web/dist/）
.venv/bin/python scripts/service.py build
```

`build` 做了两件事：
1. 每次执行 `npm ci`（避免跨操作系统复用原生 Rollup/esbuild 依赖）
2. `npm run build`（Vite 构建到 `web/dist/`）

后端是 Python 源码直读 (`python -m codeevolution.cli web`)，无需额外打包步骤。

### 自动重启

```bash
# 在服务根目录执行
cd services/codehistory

# 重启服务（stop → 构建 → start）
.venv/bin/python scripts/service.py restart --host 0.0.0.0 --port 8765
```

`restart` 流程：
1. 停止当前运行的 codeevolution 进程（SIGTERM，5s 超时等待）
2. 重新构建前端
3. 启动新进程，轮询 `/api/repos` 直到就绪
4. PID 写入 `.run/codeevolution.pid`，日志写入 `.run/codeevolution.log`

### 验证

重启后验证服务正常：

```bash
# 检查状态
.venv/bin/python scripts/service.py status

# 快速冒烟：API 健康检查
curl -s http://127.0.0.1:8765/api/repos | head -c 200
```

### 手动操作

| 命令 | 用途 |
|------|------|
| `make build` | 仅构建前端 |
| `make start` | 构建 + 启动 |
| `make stop` | 停止服务 |
| `make restart` | 停止 + 构建 + 启动 |
| `make status` | 查看运行状态 |

## 参考索引（可从代码/图谱推导，仅给入口）

- **模块结构**：见 `codeevolution/` 源码（各文件职责以 docstring 为准），用 CodeGraph / 文件树检索。
- **CodeGraph SQLite Schema**：`.codegraph/codegraph.db` 三表 `nodes` / `edges` / `files`；关键查询模式见 `codeevolution/codegraph_reader.py` 的 `CodeGraphReader`。
- **架构设计**：`docs/design.md`。

## Git 提交规则

本项目位于 `services/codehistory/`，是 `harness` 仓库下一个独立服务代码仓。提交前先 `cd` 到本目录再执行 git 操作。

## 约束

1、功能开发完成之后做API集成测试（按需Mock），UI 自动化，不做 UT
2、要考虑国际化，支持中文和英文
3、完成一个功能，有代码修改时，自动提交并重启服务
