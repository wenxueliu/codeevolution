# CodeHistory

代码仓功能演进分析 + 业务知识逆向系统。从代码自动抽取 18 维结构化知识，服务产品经理、架构师、开发、测试、运维五个角色。

## 快速开始

```bash
cd services/codehistory

# 安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 构建前端（首次启动 Web 面板前必须执行）
cd web
npm ci
npm run build
cd ..

# 安装 CodeGraph（代码解析引擎）
npm i -g @colbymchenry/codegraph

# 初始化代码图谱
(cd /path/to/your/repo && codegraph init)

# 提取知识（13 维，秒级）
.venv/bin/codehistory knowledge -r /path/to/your/repo

# 多仓微服务拓扑
.venv/bin/codehistory register -n my-svc -r /path/to/repo
.venv/bin/codehistory topology

# 将多个仓库注册为一个逻辑服务（例如前端 + 后端）
.venv/bin/codehistory register -n mall -r /path/to/mall -r /path/to/mall-admin-web

# 启动 Web 面板
.venv/bin/codehistory web --port 8765
# 浏览器访问 http://localhost:8765
```

`codehistory web` 会从 `web/dist/` 加载前端静态资源。如果修改了 `web/src/`，需要重新运行 `cd web && npm run build` 后再启动服务。前端开发时可分别启动后端 `.venv/bin/codehistory web --port 8765` 和前端 `cd web && npm run dev`，然后访问 `http://localhost:5173`。

Windows 用户请参考 [Windows (PowerShell) 安装与启动](INSTALL.md#windows-powershell)，其中包含虚拟环境路径、前端构建和 CodeGraph 初始化命令。

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
codehistory register -n <name> -r <path> [-r <path> ...]  # 注册单仓或多仓逻辑服务
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

# 渐进式重构：一次只分析一个时间窗口和一种重构手法
codehistory refactor-plan -r <repo> -t extract-method --window-days 7
# 从 7 天扩大到 14 天时，只把第 8～14 天的提交作为新候选入口
codehistory refactor-plan -r <repo> -t extract-method \
  --window-days 14 --previous-window-days 7 -o refactor-plan.json

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

## 渐进式重构计划

`refactor-plan` 从近期 Git 提交中寻找仍在频繁变化的函数，以当前 CodeGraph 图谱补全直接调用者和
依赖，并且每次只检查指定的一种重构手法。命令内置 24 种可独立选择的重构手法；使用
`codehistory refactor-plan --help` 查看完整列表。

输出是可交给编码 Agent 的结构化 JSON。如果 CodeGraph 没有找到足够的直接测试，测试门禁会阻止
生产代码修改，改为生成一个只允许添加特征测试的任务；测试安全网充分且影响风险可控时，才会生成
带文件数、符号数和修改行数预算的重构任务。当前测试充分性判断基于静态调用关系，Agent 仍需检查
断言、分支和异常覆盖质量。

Web 页面的“新增手法”和“编辑”入口可以维护检查目录。每个代码仓拥有独立配置，用户定义保存在
目标仓的 `.codehistory/refactoring-techniques.json`；编辑内置手法会保存该仓的覆盖值，不会修改源码中的
默认目录，也不会影响其他代码仓。CLI 和 Web 计划分析都会根据当前目标仓读取合并后的手法。

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

### 一键构建与启停

```bash
make start    # 首次自动 npm ci，构建前端并后台启动 :8765
make status   # 查看 PID 和日志位置
make restart  # 重新构建并重启
make stop     # 停止服务
```

Windows PowerShell 使用 `py scripts\service.py start|stop|restart|status`。运行文件保存在 `.run/`，日志为 `.run/codehistory.log`。

### 代码仓问答与审计

进入任一已注册服务后，可通过页面右侧“代码问答”打开助手。配置 `OPENAI_API_KEY`（以及可选的
`CODEHISTORY_LLM_MODEL`、`CODEHISTORY_LLM_BASE`）后，大模型会把问题转换为最多三个结构化只读操作；
未配置模型时使用本地意图识别。后端仅执行 CodeGraph 符号/调用查询和演进库功能/事件/统计白名单，
不接受模型生成的任意 SQL。每次成功或失败的操作都会写入
`~/.codehistory/assistant-audit.db`，可在对话框“审计日志”页或 `GET /api/audit-logs` 查看。

也可以通过页面顶部“LLM 设置”配置模型、API Base 和 API Key，并在保存后测试连接。页面配置默认
保存在 `~/.codehistory/llm-config.json`（或 `CODEHISTORY_DATA_DIR` 指定的目录），文件权限限制为仅当前
用户可读写，API 不会回传密钥。环境变量的优先级高于页面配置，适合部署环境统一管理凭据。

### 外部系统 UI 录制（Phase 1）

进入已注册服务后，在右侧“代码问答 → UI 测试”填写测试名称、目标名称和 HTTP(S) 地址。
CodeHistory 通过本机 Kimi WebBridge 打开独立标签页并注入录制器，可采集点击、输入、原生下拉选择、
SPA 路由和 `/api/` 网络请求。停止后生成稳定的 role/name 或 `data-testid` DSL，并可直接使用
WebBridge 回放；失败运行会保存截图。目标 origin 必须先登记，密码、Token 等敏感输入只记录为
`<redacted>`。录制数据保存在 `~/.codehistory/ui-tests.db`，测试标签页不会被自动关闭。
容器或只读 HOME 环境可通过 `CODEHISTORY_DATA_DIR` 指定可写的数据目录。

Phase 2 会通过 CDP 为整页刷新预注册录制器，并用受限的 `window.name` 缓冲区承接白名单 origin
之间尚未同步的操作；新标签页、文件选择和 HTML5 拖拽也会写入 DSL。检查点支持可见文本、URL、
接口状态、同源 fixture 请求、上传和拖拽。敏感输入回放时从录制步骤指定的环境变量读取；上传文件
必须位于 `CODEHISTORY_UI_UPLOAD_ROOT` 下。Fixture URL 和所有新标签页仍受目标 origin 白名单约束。

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

# 真实 Chrome 回归（需先启动 CodeHistory Web 和 Kimi WebBridge）
make test-ui-e2e
```

`scripts/e2e_refactoring_webbridge.py` 是渐进重构页面的真实浏览器回归用例，覆盖仓库选择、自定义手法
新增、编辑、稳定 ID 锁定、计划刷新和跨仓隔离。脚本通过公开 API 自动清理测试手法，最后一条配置
删除后不会留下空文件。重构页面、手法字段、仓库作用域或相关 API 发生变化时，必须同步更新该脚本及
`web/tests/pages.integration.spec.js` 中的组件用例。

当前重构基线：39 个测试通过，核心单元覆盖率 63.37%。Registry 和 topology cache 已采用原子写与版本化格式；旧缓存和公开入口继续兼容。

## 设计文档

- [系统设计](docs/design.md)
- [重构规划](docs/refactoring-plan.md)
- [重构实施结果](docs/refactoring-result.md)
- [功能完整性与架构优化审计](docs/architecture-audit.md)
