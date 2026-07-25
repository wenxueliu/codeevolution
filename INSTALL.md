# CodeHistory 安装指南

## 依赖

CodeHistory **不依赖** codegraph 或 code-review-graph，是完全独立的项目。

### 系统依赖

| 依赖 | 用途 | 检查命令 |
|------|------|----------|
| Python 3.10+ | 后端引擎 | `python3 --version` |
| Node.js 18+ | 前端构建 (Vue 3) | `node --version` |
| Git | 代码仓分析 | `git --version` |

### Python 包

```
tree-sitter           # AST 解析 (支持 Python/Java 及 100+ 语言)
tree-sitter-language-pack  # 语言语法包
fastapi + uvicorn     # Web API 服务器
fastmcp + mcp         # MCP 工具服务器
networkx              # 图算法
```

### Node 包 (仅前端开发/构建时需要)

```
vue + vue-router      # 前端框架
mermaid               # 时序图渲染
vite                  # 构建工具
```

## 安装

```bash
# 创建虚拟环境并安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 前端已预构建在 web/dist/，除非修改前端代码否则不需要 npm install
# 如果要修改前端：
# cd web && npm install && npm run build
```

## 不需要安装的

- **codegraph** (npm `@colbymchenry/codegraph`) — CodeHistory 有自己的 tree-sitter 解析管线，不依赖它
- **code-review-graph** (PyPI) — CodeHistory 有自己的 git walker 和 SQLite 存储，不依赖它

这两个参考项目的设计思路被借鉴（见 `docs/design.md` 第 10 节），但代码是完全独立实现的。

## 验证安装

```bash
.venv/bin/codehistory --help
# 应显示: backfill / update / web / serve / register / repos / status
```

## 快速开始

```bash
# 1. 分析一个仓库
.venv/bin/codehistory backfill --repo /path/to/your/python/project

# 2. 注册到面板
.venv/bin/codehistory register --name myproject --repo /path/to/your/python/project

# 3. 启动 Web
.venv/bin/codehistory web --port 8765
# 浏览器打开 http://localhost:8765
```
