# CodeHistory

代码仓功能演进分析系统。通过分析 git 提交历史，以"功能"（入口点 + 下游调用树）为单位追踪代码的演变过程。

## 快速开始

```bash
cd services/codehistory

# 安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 分析一个仓库
.venv/bin/codehistory backfill --repo /path/to/your/repo

# 注册到多仓面板
.venv/bin/codehistory register --name myproject --repo /path/to/your/repo

# 启动 Web 面板
.venv/bin/codehistory web --port 8765
# 打开 http://localhost:8765

# 查看状态
.venv/bin/codehistory status --repo /path/to/your/repo
```

## 功能

### 分析引擎
- **每次 commit 粒度**的增量分析，不依赖 tag
- **tree-sitter** AST 解析，支持 Python / Java
- **入口点检测**：HTTP endpoint / CLI command / event handler
- **L1 精确匹配**：跨 commit 追踪同一功能（入口点签名匹配）
- **演变事件**：BORN / GROWN / SHRUNK / MOVED / DIED 等 10+ 事件类型
- **调用链提取**：从入口点沿 CALLS 边 BFS，记录完整调用路径
- **中英文描述**：50+ 常用函数名自动生成描述

### Web 面板 (Vue 3 + Mermaid)
- **多代码仓支持**：注册多个仓库，统一面板查看
- **仪表盘**：统计卡片 + 事件类型分布 + 最近事件
- **功能列表**：搜索/状态过滤/分页 + commit 时间旅行
- **功能详情**：Mermaid 时序图（类级生命线）+ 演变时间线
- **事件日志**：全量事件查询/过滤

### CLI 命令
| 命令 | 用途 |
|------|------|
| `backfill` | 首次全量分析 |
| `update` | 增量更新 |
| `status` | 查看分析状态 |
| `register` | 注册到多仓面板 |
| `repos` | 列出已注册仓库 |
| `web` | 启动 Web 面板 |
| `serve` | 启动 MCP 服务器 |

## 技术栈

- **后端**: Python 3.10+ / tree-sitter / SQLite (WAL) / FastAPI
- **前端**: Vue 3 / Vue Router / Mermaid / Vite
- **MCP**: FastMCP

## 项目结构

```
codehistory/
├── codehistory/
│   ├── engine.py          # 核心引擎（管线编排）
│   ├── walker.py          # git 遍历 + 文件读取
│   ├── parser.py          # tree-sitter AST 解析
│   ├── matcher.py         # 功能匹配器
│   ├── analyzer.py        # 演变分析器
│   ├── store.py           # SQLite 存储
│   ├── registry.py        # 多仓注册表
│   ├── api.py             # FastAPI 后端
│   ├── mcp_server.py      # MCP 工具服务器
│   ├── cli.py             # CLI 入口
│   └── config.py          # 配置管理
├── web/                   # Vue 3 前端
│   └── src/pages/
│       ├── Home.vue           # 代码仓列表
│       ├── Dashboard.vue      # 仪表盘
│       ├── FeatureList.vue    # 功能列表
│       ├── FeatureDetail.vue  # 功能详情（含时序图）
│       └── EventList.vue      # 事件日志
└── docs/design.md         # 设计文档
```

## 设计文档

详见 [docs/design.md](docs/design.md)
