# CLAUDE.md

> **人类读者**：CodeHistory — 代码仓功能演进分析系统。设计文档见 [docs/design.md](docs/design.md)。

## 工作原则

- **尽量使用 subagent**: 执行复杂的、多步骤的、需要跨文件搜索或分析的任务时，优先通过 Agent 工具启动 subagent 来并行处理。
- **先读设计文档**: 修改代码前先理解 `docs/design.md` 中的架构决策。

## 项目概述

CodeHistory 通过分析历史 git 提交记录，以"功能"（入口点 + 下游调用树）为追踪单位，逐步建立代码仓的功能演进过程。服务开发、测试、架构三类角色。

## 参考项目

- `reference/codegraph/` — tree-sitter 解析管线、SQLite 图存储、BFS/DFS 遍历
- `reference/code-review-graph/` — git diff 解析、风险评分、社区检测

## 技术栈

- Python 3.10+ / tree-sitter / SQLite (WAL) / FastMCP
- 首次支持语言: Python, Java

## Git 提交规则

本项目位于 `services/codehistory/`，是 `harness` 仓库下一个独立的服务代码仓。提交前先 `cd` 到此目录，在 `harness` 根目录提交 services 下的变更。

## 文档导航

| 文档 | 用途 |
|------|------|
| `docs/design.md` | 完整设计文档 |
