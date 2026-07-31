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

前端已预构建在 `web/dist/`，除非修改前端代码否则不需要 `npm install`。

```bash
# 如果要修改前端：
cd web && npm install && npm run build
```

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
cd /path/to/your/project && codegraph init

# 2. 提取知识
.venv/bin/codehistory knowledge -r /path/to/your/project

# 3. 查看 Web 面板
.venv/bin/codehistory web --port 8765
# 浏览器打开 http://localhost:8765
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
