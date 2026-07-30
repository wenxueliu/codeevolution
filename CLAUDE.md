# CLAUDE.md

> **CodeHistory** — 代码仓功能演进分析 + 业务知识逆向系统。

## 工作原则

- **尽量使用 subagent**: 执行复杂的、多步骤的、需要跨文件搜索或分析的任务时，优先通过 Agent 工具启动 subagent 来并行处理。
- **先读设计文档**: 修改代码前先理解 `docs/design.md` 中的架构决策。

## 项目概述

CodeHistory 三大子系统：

1. **Evolution Engine** — 分析 git 历史，以"功能"（入口点 + 调用树）为单位追踪代码演进
2. **Knowledge Extractor** — 从代码逆向业务知识，三阶段 13 维，服务产品/架构/开发/测试/运维
3. **Cross-Repo Analyzer** — 多仓微服务统一拓扑，跨服务调用拼接 + 影响分析 + 流程追踪 + 实体对齐

## 技术栈

- Python 3.10+ / SQLite (WAL) / FastMCP / networkx / litellm (可选)
- **代码解析完全委托给 CodeGraph** (`@colbymchenry/codegraph` v0.9.x)
- 通过直接读取 `.codegraph/codegraph.db` (SQLite) 获取图谱，无需 CodeGraph 服务进程
- 使用前需在目标仓库运行 `codegraph init`
- 所有语言自动支持（CodeGraph 覆盖 30+ 语言）

## 模块架构（16 个文件）

```
codehistory/
  config.py              # 配置管理
  walker.py              # Git 历史遍历（git show 读文件对象）
  store.py               # Evolution 事件存储（SQLite WAL）
  engine.py              # 演进引擎：git checkout + codegraph sync + 特征追踪
  analyzer.py            # 快照比较 → 生成演进事件（BORN/GROWN/SHRUNK/DIED...）
  matcher.py             # L1 精确匹配 (entry_type, entry_signature) → 特征身份

  codegraph_reader.py    # CodeGraph SQLite 读取层
                         #   FunctionDef / CallTarget / EntryPointDef

  knowledge.py           # 单仓知识提取（三阶段 13 维）
  cross_repo.py          # P0 多仓拓扑：HTTP 调用拼接 + 统一拓扑 + 影响分析
  p2_advanced.py         # P2 高级分析：全通道流程追踪 + 跨服务实体对齐
  llm.py                 # LLM 调用层（litellm，支持 OpenAI / Anthropic）

  registry.py            # 多仓注册中心 + 自动检测 + 服务发现 + 健康检查 + 拓扑缓存
  api.py                 # FastAPI 后端（多仓 Web Dashboard）
  cli.py                 # CLI 入口（17 个子命令）
  mcp_server.py          # MCP Server（stdio/SSE/streamable-http）
```

## 命令参考

```bash
# ═══ 前置依赖 ═══
npm i -g @colbymchenry/codegraph    # 安装 CodeGraph
cd /path/to/target/repo
codegraph init                       # 初始化代码图谱

# ═══ Evolution Engine ═══
codehistory backfill -r /path/to/repo       # 全量回溯分析
codehistory update -r /path/to/repo         # 增量更新
codehistory status -r /path/to/repo         # 查看演进状态
codehistory serve -r /path/to/repo          # 启动 MCP Server

# ═══ Single-Repo Knowledge ═══
codehistory knowledge -r /path/to/repo                    # Phase 1+2（秒级，9 维）
codehistory knowledge -r /path/to/repo -s api             # API 契约
codehistory knowledge -r /path/to/repo -s modules         # 模块拓扑
codehistory knowledge -r /path/to/repo -s entities        # 核心实体（PageRank）
codehistory knowledge -r /path/to/repo -s tests           # 测试缺口
codehistory knowledge -r /path/to/repo -s layers          # 分层违规
codehistory knowledge -r /path/to/repo -s config          # 配置消费
codehistory knowledge -r /path/to/repo -s deps            # 外部依赖
codehistory knowledge -r /path/to/repo -s auth            # 权限模型
codehistory knowledge -r /path/to/repo -s heatmap         # 热力图
codehistory knowledge -r /path/to/repo --llm              # + Phase 3（LLM 4 维）
codehistory knowledge -r /path/to/repo -s business --llm  # 业务描述
codehistory knowledge -r /path/to/repo -s rules --llm     # 业务规则
codehistory knowledge -r /path/to/repo -s errors --llm    # 错误目录
codehistory knowledge -r /path/to/repo -s states --llm    # 状态机
codehistory knowledge -r /path/to/repo -o report.json     # 导出 JSON

# ═══ Multi-Repo Cross-Service ═══
codehistory register -n <name> -r /path/to/repo   # 注册服务（自动检测语言/角色/DB/MQ）
codehistory discover -d /path/to/repos              # 扫描目录发现 git 仓库
codehistory check                                   # 所有服务健康检查
codehistory topology                                # 统一服务拓扑（首次构建，后续缓存）
codehistory impact -s <service>                     # 跨服务变更影响（秒级缓存）
codehistory trace -s <service>                      # HTTP 调用链追踪
codehistory flow -s <service>                       # 全通道流程追踪（HTTP+MQ+gRPC）
codehistory entities [--llm]                        # 跨服务实体对齐

# ═══ Web Dashboard ═══
codehistory web                        # 启动 Web 控制台 (http://0.0.0.0:8765)
```

## CodeGraph SQLite Schema（查询参考）

CodeGraph `.codegraph/codegraph.db` 的核心表：

```sql
nodes (id, kind, name, qualified_name, file_path, language,
       start_line, end_line, start_column, end_column,
       docstring, signature, visibility,
       is_exported, is_async, is_static, is_abstract,
       decorators, type_parameters, updated_at)

edges (id, source, target, kind, metadata, line, col, provenance)
  -- kind: contains | calls | imports | exports | extends | implements |
  --        references | type_of | returns | instantiates | overrides | decorates

files (path, content_hash, language, size, modified_at, indexed_at, node_count)
```

关键查询模式见 `codegraph_reader.py` 的 `CodeGraphReader` 类。

## Git 提交规则

本项目位于 `services/codehistory/`，是 `harness` 仓库下一个独立的服务代码仓。提交前先 `cd` 到此目录，在 `harness` 根目录提交 services 下的变更。
