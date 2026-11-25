---
status: proposed
date: 2026-11-25
decision-makers:
consulted:
informed:
---

# Record architecture decisions as an ADR knowledge dimension

## Context and Problem Statement

CodeEvolution 从代码逆向业务知识，但「为什么这样实现」只存在于提交历史和人的记忆里。
仓库中已经常见的 MADR / adr-tools 决策记录没有被纳入知识体系：知识中心缺少与 API 契约
同级的架构决策视角，产品、架构和开发只能从代码反推意图。ADR 又天然是仓库数据（Markdown +
frontmatter），不适合塞进 CodeGraph。

## Decision Drivers

* 决策记录应与 API 契约同为一级知识维度，可在同一 Snapshot 中查询；
* 生成、索引与 CI 门禁已经由 `sw-adr-engineering` 技能提供，避免重复实现；
* 知识提取必须只读、可复现，不因为文档格式问题让分析失败；
* 兼容存量仓库：已用 adr-tools（`## Status`）或 MADR 3.x 的仓库无需迁移。

## Considered Options

* 把 ADR 解析与 R1/R2 校验移植进 `analysis/knowledge/adr.py`，随 Snapshot facts 冻结；
* 运行时调用外部 `adr-check` / `adr-drift` 二进制；
* 只在 Web 端展示仓库目录，不进入 facts。

## Decision Outcome

Chosen option: "移植到 `analysis/knowledge/adr.py`", because 它保持零依赖（PyYAML 可选、
stdlib 兜底），与其它维度共享 facts 冻结与 Snapshot 查询边界，并且不引入运行时外部进程。
分析发布时探测 `docs/adr`、`doc/adr`、`docs/decisions`、`adr`，解析 MADR 4.0 frontmatter
（含旧式 `## Status` 回退），执行 R1（结构）与 R2（替代关系）校验。生成、索引刷新与
CI 门禁仍由 `sw-adr-engineering` 技能负责，CodeEvolution 只读不改写决策记录。

### Consequences

* Good, because 决策记录与 API 契约处于同一查询/展示层级（`-s adr`、知识中心分区）。
* Good, because 旧 Snapshot 没有 ADR facts 时返回 `available: false`，不破坏历史读取。
* Bad, because ADR 文件不在冻结的源码清单中，分析时从 checkout 读取，和分层声明一样
  需要在生成 Snapshot 前提交。

### Confirmation

`tests/test_adr_knowledge.py` 覆盖发布 facts、HTTP `/api/knowledge?section=adr` 与旧
Snapshot 的降级返回；`docs/design.md` 的「ADR 决策记录（adr/v1）」一节记录输入与校验规则。

## Pros and Cons of the Options

### 移植进 `analysis/knowledge/adr.py`

* Good, because 无外部进程、无网络调用，随 facts 冻结可复现。
* Neutral, because 需要跟随上游 adrkit 的规则演进手动同步。
* Bad, because 只做只读抽取，不承担生成与索引写入。

### 调用外部 `adr-check` / `adr-drift`

* Good, because 与 `sw-adr-engineering` 行为完全一致。
* Bad, because 增加运行时依赖（pip 包、PATH），并让 Snapshot 分析依赖外部工具版本。

### 只在 Web 端展示目录

* Good, because 实现成本最低。
* Bad, because 无法进入 facts、CLI、MCP 与跨仓视图，达不到「与 API 契约平级」。

## More Information

* 规则来源：`sw-adr-engineering` 技能的 `references/adr-rules.md`（R1/R2）。
* 模板：`docs/adr/template/adr-template.md`（MADR 4.0 bare）。
