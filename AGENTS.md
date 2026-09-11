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

# 多仓分析（先 register 注册服务）
codeevolution register -n <name> -r <repo>
codeevolution topology --view-id <view-id>
codeevolution impact --view-id <view-id> --service <member-id>
codeevolution flow --view-id <view-id> --service <member-id> --entry-id <entry-id>

# Web 控制台
codeevolution web                    # http://0.0.0.0:8765
```

完整命令以 `codeevolution --help` 与 `codeevolution/cli.py` 为准；旧的
`backfill`、`update`、演进 `status` 和 `init-all` 已移除。

## 能力矩阵（18 维 × 5 角色，兼作命令速查）

| # | 能力 | 产品 | 架构 | 开发 | 测试 | 运维 | 命令 |
|---|------|:---:|:---:|:---:|:---:|:---:|------|
| 1 | API 契约 | x | x | x | x | | `knowledge --snapshot-id <id> -s api` |
| 2 | 模块拓扑 | | x | x | | | `knowledge --snapshot-id <id> -s modules` |
| 3 | 核心实体 (PageRank) | | x | x | | | `knowledge --snapshot-id <id> -s entities` |
| 4 | 测试缺口 | | | | x | | `knowledge --snapshot-id <id> -s tests` |
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
