# CodeHistory

代码仓功能演进分析 + 业务知识逆向系统。从代码自动抽取 18 维结构化知识，服务产品经理、架构师、开发、测试、运维五个角色。

## 快速开始

```bash
cd services/codehistory

# 安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 安装 CodeGraph（代码解析引擎）
npm i -g @colbymchenry/codegraph

# 初始化代码图谱
cd /path/to/your/repo && codegraph init

# 提取知识（13 维，秒级）
.venv/bin/codehistory knowledge -r /path/to/your/repo

# 多仓微服务拓扑
.venv/bin/codehistory register -n my-svc -r /path/to/repo
.venv/bin/codehistory topology

# 启动 Web 面板
.venv/bin/codehistory web --port 8765
```

## 三大子系统

### 1. Knowledge Extractor — 单仓知识提取（13 维）

纯图计算 + 规则匹配 + LLM 语义理解，不需要重新解析代码：

| 阶段 | 维度 | 依赖 |
|------|------|------|
| Phase 1 | API 契约 / 模块拓扑 / 核心实体 / 测试缺口 / 分层违规 | CodeGraph SQLite + networkx |
| Phase 2 | 配置消费 / 外部依赖 / 权限模型 / 热力图 | CodeGraph SQLite + 规则引擎 |
| Phase 3 | 业务描述 / 业务规则 / 错误目录 / 状态机 | CodeGraph SQLite + litellm (LLM) |

### 2. Cross-Repo Analyzer — 多仓微服务分析（5 维）

跨代码仓拼接服务间调用关系，支持 HTTP + MQ + gRPC 全通道分析：

| 能力 | 命令 |
|------|------|
| 统一服务拓扑 | `topology` |
| 跨服务变更影响 | `impact -s <svc>` |
| 全通道流程追踪 | `flow -s <svc>` |
| 跨服务实体对齐 | `entities [--llm]` |
| 服务发现+健康检查 | `discover` / `check` |

### 3. Evolution Engine — 代码演进追踪

沿 git 历史逐 commit 分析，以"功能"为单位追踪代码演变过程。

## 能力矩阵（18 维 × 5 角色）

| # | 能力 | 产品 | 架构 | 开发 | 测试 | 运维 | 命令 |
|---|------|:---:|:---:|:---:|:---:|:---:|------|
| 1 | API 契约 | x | x | x | x | | `knowledge -s api` |
| 2 | 模块拓扑 | | x | x | | | `knowledge -s modules` |
| 3 | 核心实体 | | x | x | | | `knowledge -s entities` |
| 4 | 测试缺口 | | | | x | | `knowledge -s tests` |
| 5 | 分层违规 | | x | | | | `knowledge -s layers` |
| 6 | 配置消费图 | | | | | x | `knowledge -s config` |
| 7 | 外部依赖清单 | | x | | | x | `knowledge -s deps` |
| 8 | 权限模型 | | x | | | x | `knowledge -s auth` |
| 9 | 热力图 | | x | x | | | `knowledge -s heatmap` |
| 10 | 业务描述 | x | | x | | | `knowledge -s business --llm` |
| 11 | 业务规则 | x | | | x | | `knowledge -s rules --llm` |
| 12 | 错误目录 | | | | x | x | `knowledge -s errors --llm` |
| 13 | 状态机 | x | | | x | | `knowledge -s states --llm` |
| 14 | 统一服务拓扑 | | x | x | | x | `topology` |
| 15 | 跨仓变更影响 | | x | x | x | | `impact -s <svc>` |
| 16 | 全通道流程追踪 | | x | x | x | x | `flow -s <svc>` |
| 17 | 跨服务实体对齐 | | x | x | | | `entities [--llm]` |
| 18 | 服务发现+健康检查 | | | | | x | `discover` / `check` |

## CLI 命令总览

```bash
# 单仓知识提取
codehistory knowledge -r <repo> [-s api|modules|entities|tests|layers|config|deps|auth|heatmap] [--llm]
codehistory knowledge -r <repo> -s business|rules|errors|states --llm

# 多仓微服务
codehistory register -n <name> -r <path>    # 注册服务（自动检测语言/角色/DB/MQ）
codehistory discover -d <dir>               # 扫描目录发现 git 仓库
codehistory init-all                        # 一键初始化所有服务的 CodeGraph
codehistory check                           # 所有服务健康检查
codehistory topology                        # 统一服务拓扑（缓存加速）
codehistory impact -s <svc>                 # 跨服务变更影响（秒级缓存）
codehistory trace -s <svc>                  # HTTP 调用链追踪
codehistory flow -s <svc>                   # 全通道流程追踪
codehistory entities [--llm]                # 跨服务实体对齐

# 演进引擎
codehistory backfill -r <repo>              # 全量回溯分析
codehistory update -r <repo>                # 增量更新
codehistory status -r <repo>                # 查看演进状态

# 其他
codehistory serve -r <repo>                 # 启动 MCP Server
codehistory web                             # 启动 Web 控制台 (:8765)
```

## 技术栈

- **后端**: Python 3.10+ / SQLite (WAL) / FastAPI / networkx / litellm (可选)
- **代码解析**: 完全委托给 CodeGraph (`@colbymchenry/codegraph` v0.9.x)，直接读其 SQLite
- **前端**: Vue 3 / Vue Router / Mermaid / Vite
- **MCP**: FastMCP
- 所有 30+ 语言自动支持，无需配置

## 模块架构

```
codehistory/
├── domain/              # 纯领域 DTO
├── ports.py             # CodeGraph Repository / SourceProvider 契约
├── infrastructure/      # SQLite、源码、Registry、版本化缓存 adapter
├── analysis/
│   ├── knowledge/       # 独立知识维度与 report builder
│   └── topology/        # topology / impact / flow / 纯 matcher
├── application/         # Evolution / Knowledge / Topology / Repository 用例
├── delivery/            # renderer 与交付适配边界
├── semantic/            # LLM config / client / JSON parser / models / service
├── engine.py            # 演进引擎
├── walker.py / store.py # Git 遍历 + Evolution SQLite
└── cli.py / api.py / mcp_server.py
```

依赖方向固定为：

```text
delivery → application → analysis/domain/ports ← infrastructure
```

`codegraph_reader.py`、`knowledge.py`、`cross_repo.py`、`p2_advanced.py`、`registry.py` 和 `llm.py` 保留原有公开入口，作为渐进迁移期间的兼容 facade。

## 开发与验证

```bash
# 静态检查
.venv/bin/ruff check codehistory tests scripts/check_coverage.py

# 单元与契约测试
.venv/bin/python -m pytest -q

# 核心单元覆盖率门禁（fail-under=60）
.venv/bin/python scripts/check_coverage.py

# Python / Web 构建验证
.venv/bin/python -m build
cd web && npm run build
```

当前重构基线：39 个测试通过，核心单元覆盖率 63.37%。Registry 和 topology cache 已采用原子写与版本化格式；旧缓存和公开入口继续兼容。

## 设计文档

- [系统设计](docs/design.md)
- [重构规划](docs/refactoring-plan.md)
- [重构实施结果](docs/refactoring-result.md)
- [功能完整性与架构优化审计](docs/architecture-audit.md)
