# CodeHistory 重构实施结果

> 完成日期：2026-08-01  
> 对应规划：[refactoring-plan.md](refactoring-plan.md)

## 结果摘要

重构按阶段 A–I 渐进完成，保留原有 Python import、CLI、API、MCP 和运行入口。新的核心依赖方向为：

```text
delivery → application → analysis/domain/ports ← infrastructure
```

主要成果：

- 领域 DTO 移至 `domain/`，旧模块继续 re-export。
- `SourceProvider`、`CodeGraphRepository` 成为显式端口。
- CodeGraph SQLite、源码读取、Registry 和 topology cache 进入 `infrastructure/`。
- Registry 与 cache 使用临时文件和 `os.replace()` 原子写入。
- topology cache 带 `schema_version`，能够读取无版本旧缓存。
- 知识维度、拓扑构建、影响分析、流程追踪和匹配规则进入 `analysis/`。
- Evolution、Knowledge、Topology、Repository 用例进入 `application/`。
- CLI 使用 parser handler 分派；API 提供 `create_app(dependencies)` 与 lifespan；MCP 开始复用应用服务。
- LLM 配置、client、JSON parser、模型和服务进入 `semantic/`。
- 修复 caller 名称、调用树深度、call-chain `from` 和 LLM 批处理并发限制。

## 目录结构

```text
codehistory/
├── domain/                     # 纯 DTO
├── ports.py                    # Repository / SourceProvider Protocol
├── infrastructure/            # SQLite、文件系统、Registry、缓存 adapter
├── analysis/
│   ├── knowledge/             # 独立知识维度与 report builder
│   └── topology/              # builder、impact、flow、纯 matcher
├── application/               # 共享用例服务
├── delivery/                  # renderer 与交付边界
├── semantic/                  # LLM config/client/parser/models/service
├── codegraph_reader.py        # 兼容 facade
├── knowledge.py               # 兼容 facade
├── cross_repo.py              # 兼容 facade
├── p2_advanced.py             # 兼容 facade
├── registry.py                # 兼容函数入口
├── llm.py                     # 兼容函数入口
├── cli.py
├── api.py
└── mcp_server.py
```

## 兼容性

以下入口继续有效：

- `codehistory.knowledge.KnowledgeExtractor`
- `codehistory.codegraph_reader.CodeGraphReader`、原有 DTO import
- `codehistory.cross_repo.CrossRepoAnalyzer`
- `codehistory.p2_advanced.P2Analyzer`
- `registry.py` 原有函数
- `api.app`、`create_app()` 和 `serve()`
- `mcp_server.run_server()` 及原有五个 tool
- 现有 16 个 CLI 命令、参数和退出码

## 验证门禁

在服务根目录执行：

```bash
.venv/bin/ruff check codehistory tests scripts/check_coverage.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_coverage.py
.venv/bin/python -m compileall -q codehistory
cd web && npm run build
```

本次实施的最终结果：

- Ruff：通过
- Tests：`39 passed`
- 核心单元覆盖率：`63.37%`，门禁为 `60%`
- Python `compileall`：通过
- Web production build：通过

覆盖率门禁统计 `domain/analysis/application/infrastructure/semantic` 与演进核心；兼容 facade 和交付 adapter 使用 contract/integration tests 验证，不计入核心单元覆盖率分母。

标准的 `python -m build` 仍是发布前门禁。本次离线环境未安装 `build`/`hatchling`，因此使用 `compileall` 完成代码级验证；发布环境应安装 `.[dev]` 后再次运行正式包构建。

## 提交序列

重构没有压成单个大提交。主要提交依次为：

1. `test: add characterization coverage for public contracts`
2. `refactor: extract domain models and source provider`
3. `refactor: introduce codegraph repository`
4. `refactor: split knowledge extractors`
5. `refactor: split topology and flow analysis`
6. `refactor: isolate registry and cache infrastructure`
7. `refactor: add shared application services`
8. `refactor: modularize CLI API and MCP adapters`
9. `refactor: isolate semantic LLM services`
10. `test: enforce unit coverage above sixty percent`
11. 独立的调用图和 LLM 并发行为修复提交

