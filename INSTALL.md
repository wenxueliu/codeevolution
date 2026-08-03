# CodeHistory 安装指南

## 依赖

### 系统依赖

| 依赖 | 用途 | 检查命令 |
|------|------|----------|
| Python 3.10+ | 后端引擎 | `python3 --version` |
| Node.js 18+ | CodeGraph + 前端构建 | `node --version` |
| Git | 代码仓分析 | `git --version` |

### CodeGraph（必须）

CodeHistory 的代码解析完全委托给 CodeGraph。**每个要分析的目标仓库都需要先初始化 CodeGraph**。

```bash
npm i -g @colbymchenry/codegraph
cd /path/to/target/repo
codegraph init
```

CodeHistory 通过直接读取 `.codegraph/codegraph.db`（SQLite WAL）获取代码图谱，不需要启动 CodeGraph 服务进程。所有语言自动支持（CodeGraph 覆盖 30+ 语言）。

### Python 包

```
fastapi + uvicorn     # Web API 服务器
fastmcp + mcp         # MCP 工具服务器
networkx              # 图算法（PageRank / Louvain 社区检测）
```

可选：
```
litellm               # LLM 支持（Phase 3 知识提取）
```

### Node 包（仅前端开发/构建时需要）

```
vue + vue-router      # 前端框架
mermaid               # 时序图渲染
vite                  # 构建工具
```

## 安装

```bash
cd services/codehistory

# 创建虚拟环境并安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 安装 LLM 支持（可选）
.venv/bin/pip install -e ".[llm]"
```

首次启动 Web 面板前需要安装前端依赖并生成 `web/dist/`：

```bash
cd web
npm ci
npm run build
cd ..
```

修改 `web/src/` 后需要重新执行 `cd web && npm run build`，否则后端仍会加载上一次构建的静态资源。

## 验证安装

```bash
.venv/bin/codehistory --help
# 应显示: backfill / update / status / register / repos / web / serve
#         / knowledge / topology / impact / trace / flow / entities
#         / discover / check / init-all
```

## LLM 配置（可选）

Phase 3 知识提取（业务描述/规则/错误目录/状态机）需要 LLM：

```bash
export OPENAI_API_KEY="sk-..."
# 或
export ANTHROPIC_API_KEY="sk-ant-..."

# 可选：覆盖默认模型
export CODEHISTORY_LLM_MODEL="gpt-4o-mini"
```

## 快速开始

```bash
# 1. 初始化 CodeGraph
(cd /path/to/your/project && codegraph init)

# 2. 提取知识
.venv/bin/codehistory knowledge -r /path/to/your/project

# 3. 查看 Web 面板
.venv/bin/codehistory web --port 8765
# 浏览器打开 http://localhost:8765
```

`codehistory web` 从 `web/dist/` 提供前端页面。如果访问根路径时只看到 `Frontend not built`，请回到 CodeHistory 目录执行 `cd web && npm ci && npm run build`，再重启后端。

## Windows (PowerShell)

先安装 64 位 Python 3.10+、Node.js 18+ 和 Git，并确认它们已加入 `PATH`：

```powershell
py --version
node --version
npm --version
git --version
```

在 CodeHistory 目录中安装后端与前端依赖：

```powershell
cd services\codehistory

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Push-Location web
npm ci
npm run build
Pop-Location

npm install --global @colbymchenry/codegraph
```

不需要执行 `Activate.ps1`，因此不会受 PowerShell 脚本执行策略影响。如果需要 LLM 支持，另行执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[llm]"
```

为目标仓库初始化 CodeGraph，然后提取知识并启动 Web 面板：

```powershell
$TargetRepo = "C:\path\to\your\repo"

Push-Location $TargetRepo
codegraph init
Pop-Location

.\.venv\Scripts\codehistory.exe knowledge -r $TargetRepo
.\.venv\Scripts\codehistory.exe web --port 8765
```

浏览器访问 `http://localhost:8765`。如果修改了前端，在 CodeHistory 目录重新执行 `Push-Location web; npm run build; Pop-Location`，再重启后端。

可选的 LLM 环境变量可在当前 PowerShell 会话中设置：

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:CODEHISTORY_LLM_MODEL = "gpt-4o-mini"
```

## 多仓微服务设置

```bash
# 注册多个服务
.venv/bin/codehistory register -n order-svc -r /repos/order-service
.venv/bin/codehistory register -n user-svc  -r /repos/user-service

# 一键初始化所有服务的 CodeGraph
.venv/bin/codehistory init-all

# 查看统一拓扑
.venv/bin/codehistory topology
```
