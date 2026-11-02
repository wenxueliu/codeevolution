# Domain Evidence Graph 执行计划（基于当前实现）

> 配套设计：`docs/domain-evidence-graph-design.md`（V1.1 Final）
> 现状基线：`docs/term-recognition-design.md` + 当前 `codeevolution` 代码
> 日期：2026-11-03
> 状态：待确认后实施

---

## 0. 结论摘要

1. 目标方案（9 模块 + 证据图谱 + LLM + 治理发布）方向正确，但**不能按原文档直接新建一套 TypeScript/PostgreSQL/React 系统**。当前工程是 Python + SQLite(WAL) + FastAPI + Vue，术语能力已有可用基线，正确做法是在现有 `term_*` 模型上做**兼容演进**。
2. 现有实现只覆盖目标方案的 **模块 01/02 的一部分 + 模块 03 的最小权重版 + 模块 05 的仓内同名合并**；模块 04 校准、06 语义、07 领域图谱、08 治理发布、09 增量幂等基本缺失。
3. 建议按「**证据可信度优先，治理能力后置**」推进，分 5 个里程碑（对应设计文档 P1–P4，另加 Phase 0 基线）。
4. 存储建议：**MVP 继续使用 SQLite**，通过仓储接口隔离；PostgreSQL + pgvector 作为规模化阶段的可选迁移，不进入 MVP 关键路径。
5. 实施中每个功能完成即按仓库约束自动提交并重启服务；API 集成测试 + UI 自动化，不写 UT。

---

## 1. 当前实现基线

| 能力 | 当前实现 | 位置 |
| --- | --- | --- |
| 快照事实 | `KnowledgeExtractor.extract_all()` 产出 `api_contract`、`core_entities`、`module_topology`、`frontend_pages` 等冻结事实 | `codeevolution/knowledge.py:121` |
| 术语抽取 | `TermRecognizer.extract()` 规则抽取 page / entity / resource / action 四类 | `codeevolution/analysis/knowledge/terms.py:146` |
| 名称归一化 | 分词、单数化、CRUD/技术词/泛化词剥离 | `codeevolution/analysis/knowledge/terms.py:76-116` |
| 证据 | `TermEvidence(evidence_type, value, weight, source_location, rule_id, node_id)` | `codeevolution/domain/terms.py:9` |
| 置信度 | 证据权重线性求和 + `domain_score*0.12`，单分数 | `codeevolution/analysis/knowledge/terms.py:447` |
| 关系 | `exposes`（resource→entity）、`applies_to`（action→noun） | `codeevolution/analysis/knowledge/terms.py:475` |
| 持久化 | SQLite `terms.db`：terms / term_snapshot_results / term_evidence / term_relations / term_alignments / term_overrides | `codeevolution/infrastructure/term_store.py:39` |
| 人工审核 | accept / reject / rename / reclassify / alias / relate；手工新增术语 | `codeevolution/infrastructure/term_store.py:248/315/358` |
| 跨服务对齐 | `align()` 仅按名称相似度（≥0.85 same，0.6–0.85 related），要求同类型、服务已连接、非技术名 | `codeevolution/application/term_service.py:69` |
| 自动触发 | 快照发布后 `_generate_terms()` 异步派生，失败不影响快照 | `codeevolution/application/repository_attempt_worker.py:364` |
| API | `/api/terms/extract`、`/api/terms`、`/api/terms/{id}`、`/evidence`、`/review`、`/manual`、`/align` | `codeevolution/api.py:1815-1930` |
| Web | Terms 页 + Knowledge 页术语区（中英 i18n） | `web/src/pages/Terms.vue`、`Knowledge.vue` |
| LLM 基建 | `LLMKnowledgeService` + `llm_config_store` + `openai` 可选依赖 | `codeevolution/application/llm_knowledge_service.py` |

已确认的现状问题：

- `term_overrides` 只写不读，人工 override 不会在重新抽取时生效。
- 单一 `confidence_score` 同时承担「是不是术语」和「关系是否成立」，无法分离。
- 证据无 `polarity`、`reliability`、`independence_group`、`content_hash`、`derived_from`、`source_version`。
- 证据权重是全局常量，未按 `术语类型 × 证据类型` 区分。
- 无 DB Schema、ORM、消息事件、Enum、Command、Attribute 来源。
- 无 Bounded Context，跨服务对齐只按名称。
- 无成对判断、合并操作、回滚。
- 无定义生成、无 FACT/INFERENCE/UNKNOWN、无 Grounding。
- 无发布快照、无词典版本、无增量幂等、无审计。
- 现有术语类型与目标类型未对齐：当前 `resource/action/page/entity`，目标 `ENTITY/BUSINESS_EVENT/BUSINESS_ACTION/BUSINESS_ATTRIBUTE/BUSINESS_ENUM/COMMAND/API_RESOURCE/DTO/TECHNICAL_COMPONENT`。

---

## 2. 差距矩阵

| 设计模块 | 当前覆盖 | 差距 | 优先级 |
| --- | --- | --- | --- |
| 01 Asset Discovery | 仓库/快照/跨仓拓扑已有；模块拓扑已有 | 服务职责、语言、DB/消息资产登记、Bounded Context 候选 | P1 |
| 02 Candidate Mining | API、核心实体、前端页面 | DB Schema/列、ORM、Enum、Event、Command、DTO、Attribute、Service Method；P0–P2 优先级 | P0 |
| 03 Evidence Fusion | 单条 evidence + 固定 weight | 统一 Evidence 字段、极性、可靠性、独立性、内容指纹、冲突隔离 | P0 |
| 04 Confidence | 单一加权和 | Term/Definition/Relation 三类置信度、分组聚合、阈值、校准 | P1 |
| 05 Normalization | 仓内按 normalized/canonical 合并；跨服务名称相似 | Pairwise 判断、硬/软/复核约束、ALIAS/投影/特化、merge_operation、补偿回滚 | P1 |
| 06 Semantic | 无（仅手工定义） | Evidence Package → LLM 定义、FACT/INFERENCE/UNKNOWN、四项 Grounding、可复现记录 | P2 |
| 07 Domain Graph | `exposes` / `applies_to` | HAS_ATTRIBUTE/PERFORMS/TRIGGERS/CONSUMES 等业务关系、Bounded Context 隔离 | P2 |
| 08 Governance | accept/reject + 手工术语 | 角色、审核队列、术语版本、不可变发布快照、差异、审计 | P3 |
| 09 Evolution | 每次快照全量重算 | Commit 增量、幂等键、analysis_run、版本体系 | P3 |

---

## 3. 技术栈适配决策

| 设计基线 | 当前工程 | 建议 |
| --- | --- | --- |
| TypeScript / Node.js | Python 3.10+ | **沿用 Python**，不重写 |
| PostgreSQL + pgvector | SQLite(WAL) + FTS5 | **MVP 沿用 SQLite**；仓储接口隔离，规模化再迁 PG |
| BullMQ + Redis | 进程内线程 / 现有调度器 | 复用 `AnalysisScheduler`、`GraphArtifactScheduler` 模式，不引入 Redis |
| React | Vue 3 + Vite | 沿用 Vue，扩展 Terms 页 |
| Tree-sitter | CodeGraph（30+ 语言） | **优先 CodeGraph**；仅当某语言缺少所需 AST 事实时再接 Tree-sitter 适配器 |
| LLM Structured Output | `openai` 可选依赖 + 现有 LLM 配置 | 复用 `LLMConfigStore` / `LLMKnowledgeService` 模式，新增术语定义服务 |
| 独立图数据库 | 无 | 继续用关系表 + 有限遍历；抽象 Node/Edge 模型 |

---

## 4. 分阶段执行计划

### Phase 0：契约与迁移基线（约 3–5 天）

目标：先让数据模型能承载目标方案，避免后续反复改表。

- 扩展 `codeevolution/domain/terms.py`：
  - `TermEvidence` 增加 `evidence_id`、`polarity`、`reliability`、`independence_group`、`content_hash`、`derived_from`、`source_version`、`observed_at`、`subject_id`、`predicate`、`object_value`。
  - `TermCandidate` 增加 `priority`、`language`、`module`、`source_refs`、`term_confidence`、`definition_confidence`、`lifecycle_status`、`version`。
  - 新增 `TermDefinition`（FACT/INFERENCE/UNKNOWN）、`EvidencePackage`、`EvidenceConflict`、`TermPairJudgment`、`MergeOperation`、`ReleaseSnapshot`、`ReleaseItem`、`AnalysisRun`。
- 统一类型枚举：`ENTITY / BUSINESS_EVENT / BUSINESS_ACTION / BUSINESS_ATTRIBUTE / BUSINESS_ENUM / COMMAND / API_RESOURCE / DTO / TECHNICAL_COMPONENT / PAGE`，并提供旧类型映射（`resource→API_RESOURCE`、`action→BUSINESS_ACTION`、`page→PAGE`、`entity→ENTITY`）。
- `TermStore._migrate()` 增加新表与新列（SQLite `ALTER TABLE` + `CREATE TABLE IF NOT EXISTS`），保持旧快照可读。
- 增加 `analysis_version` / 规则版本常量；术语规则权重外置到 `.codeevolution/terms.json`（可缺省）。
- 修复 `term_overrides`：抽取时读取 `accept/reject/rename/alias` override 并应用（当前只写不读）。
- 建立术语 API 集成测试基线（Mock LLM，不写 UT）。

交付物：新数据契约、可向前兼容的 SQLite 迁移、override 生效。
Gate：旧快照 `/api/terms` 返回不回归。

### Phase 1（对应设计 P1，第 1–2 周）：资产发现 + 候选挖掘

- 新增 `AssetDiscovery`：从快照 facts 汇总 repository/service/module/language/API/DB/messaging 资产清单；模块来自 `module_topology`，语言来自冻结文件清单。
- 新增候选来源抽取器（优先读 CodeGraph SQLite，必要时读冻结源码）：
  - `DbSchemaExtractor`：SQL/ORM/迁移文件 → 表、列、主键、唯一键。
  - `MessageContractExtractor`：Kafka/RabbitMQ topic/exchange/queue、producer/consumer、payload 类型。
  - `EnumExtractor`：枚举及成员（业务状态）。
  - `CommandExtractor` / `DtoExtractor` / `EventExtractor`：按命名后缀 + 类型关系分类，避免把 DTO 当 Entity。
  - `AttributeExtractor`：实体/DTO 字段 → BUSINESS_ATTRIBUTE 候选。
  - `ServiceMethodExtractor`：公开业务方法 → BUSINESS_ACTION 候选（P1）。
- 扩展 `TermRecognizer`：P0/P1/P2 优先级、语言与模块、源定位、规则版本；保留现有 page/entity/resource/action 行为作为兼容路径。
- 输出 `CandidateTerm` 新字段；候选必须 100% 可追溯到 `file/symbol/schema + source_version`。

交付物：资产清单、候选术语库、来源定位索引。
Gate G1：P0 候选来源可追溯率 100%。

### Phase 2（对应设计 P2，第 3–4 周）：证据融合 + 置信度

- 新增 `EvidenceFusionService`：
  - 证据分类：Structural / Identity / Persistence / Contract / Event / Behavior / Lifecycle / Ownership / Documentation。
  - 按 `术语类型 × 证据类型 × 上下文` 查可靠性表（规则配置）。
  - `content_hash` 去重、`independence_group` 分组、`derived_from` 直接派生、`source_version` 校验。
  - 冲突分类与隔离：同名异义、映射错误、版本漂移、权威源冲突、共享字段滥用、事实矛盾；未解决冲突禁止自动合并/发布。
- 新增 `ConfidenceService`：Term / Definition / Relation 三类独立置信度；按独立性分组聚合支持/反对证据；阈值 0.90 / 0.70 / 0.40。
- 标注集与校准：`docs` 下的标注样本格式 + 离线评估脚本（ECE / Brier / 分桶准确率）；MVP 先记录、不强制上线校准模型。
- 持久化：`evidence_package`、`evidence_conflict`，扩展 `term_evidence`。

交付物：Evidence Package、冲突报告、三类置信度。
Gate G2：同源证据去重、关键冲突可识别并隔离。

### Phase 3（对应设计 P3，第 5–6 周）：概念归一化 + 语义分析

- `PairwiseMatcher`：同类型候选两两判断，输出 `SAME_CONCEPT / ALIAS / CONTEXT_PROJECTION / SPECIALIZATION / RELATED / DISTINCT / UNCERTAIN`；保存双方 ID、证据、置信度、算法版本、审核状态。
- 三类约束：Hard（跨上下文且无映射证据、身份/生命周期根本冲突、Owner 已否决、版本不兼容）→ 禁止自动合并；Soft → 影响分数；Review → 强制人工。
- `merge_operation`：合并/撤销合并（REVERSE_MERGE）独立记录，事务内完成关系迁移，保留原术语与历史关系。
- LLM 定义服务（复用现有 LLM 配置）：
  - 输入仅 Evidence Package，不直接喂整仓代码。
  - 输出 FACT / INFERENCE / UNKNOWN；FACT 必须绑定 `evidence_id`。
  - 四项 Grounding：Evidence Existence、Version Consistency、Locator Validity、Contradiction Check。
  - 记录 model / prompt_version / evidence_package_version / analysis_version / input_hash / timestamp / structured_output / grounding_result；输入脱敏与权限检查。
- 定义状态：草案 → 待审核；Grounding 失败只能保留草案。

交付物：标准术语候选、定义草案、归一化审核队列。
Gate G3：硬约束有效、合并可审计可回滚。
Gate G4：FACT 引用有效、版本与定位校验通过。

### Phase 4（对应设计 P4，第 7–8 周）：治理 + 发布 + 增量演进

- 治理：Owner / Steward / Reviewer 角色字段；审核记录（审核人、意见、时间、对象版本、决策）；复用 `AuditStore` 记录关键操作。
- 不可变发布：`release_snapshot` + `release_item`，发布门禁（证据有效、版本一致、定位可解析、无未解决冲突、上下文明确、Owner 审核、合并有记录、无敏感数据）；发布后禁止修改；回滚 = 基于历史快照创建新发布。
- 词典差异比较接口与页面。
- 增量：`analysis_run` 表 + 幂等键 `H(repo, commit, analysis_version, input_hash)` 唯一约束；以 Commit 变更文件 → 受影响 Symbol → 受影响术语/证据/关系为范围重算；失败可重试，仅成功结果可复用。
- Web：
  - Terms 页扩展：证据面板（支持/反对/冲突）、三类置信度、定义草案、归一化队列、发布与差异。
  - 所有新增文案进 `web/src/i18n.js`，中英双语，动态值用占位符。
- API：新增 evidence-package、pair-judgment、merge、definition、release、diff、incremental 相关端点；保持现有端点兼容。

交付物：正式业务词典、领域图谱、审核发布流程、增量分析能力。
Gate G5：Owner 审核完成、发布快照不可变且可验证。

---

## 5. 数据模型变更清单（SQLite，兼容演进）

新增表：

- `bounded_context`
- `asset_inventory`
- `candidate_term`（或扩展 `terms` + `term_snapshot_results`）
- `evidence_package`
- `evidence_conflict`
- `term_definition`
- `term_pair_judgment`
- `merge_operation`
- `review_record`
- `release_snapshot` / `release_item`
- `analysis_run`

扩展表：

- `term_evidence`：`polarity`、`reliability`、`independence_group`、`content_hash`、`derived_from`、`source_version`、`observed_at`、`subject_id`、`predicate`、`object_value`。
- `terms`：`display_name`、`domain_id`、`bounded_context_id`、`source_of_truth`、`term_confidence`、`definition_confidence`、`owner`、`steward`、`lifecycle_status`、`version`、`effective_time`、`deprecated_reason`、`replaced_by`。
- `term_snapshot_results`：保留旧字段，新增 `priority`、`language`、`module`。
- `term_relations`：`direction`、`cardinality`、`bounded_context_id`、`review_status`、`version`。

迁移原则：

- 所有新列可空或有默认值；旧快照可继续读取。
- 旧类型值保留，查询层做映射；新写入使用新类型。
- 发布快照与证据不可变；更新通过新版本记录。

---

## 6. 接口与 UI 变更

API（新增，保持现有兼容）：

- `GET /api/terms/{term_id}/evidence-package`
- `GET /api/terms/pair-judgments` / `POST /api/terms/pair-judgments/{id}/review`
- `POST /api/terms/merge` / `POST /api/terms/merge/{id}/reverse`
- `POST /api/terms/{term_id}/definition`（LLM 生成）/ `GET /api/terms/{term_id}/grounding`
- `POST /api/releases` / `GET /api/releases` / `GET /api/releases/{id}` / `GET /api/releases/{id}/diff`
- `POST /api/analysis-runs`（增量）/ `GET /api/analysis-runs/{id}`

Web：

- Terms 页：候选/证据/定义/归一化/发布分栏；保留现有列表与审核操作。
- Knowledge 页术语区保持只读概览。
- 新增发布差异视图。
- 中英文案同步补齐；语言切换不重置当前路由与异步任务。

---

## 7. 测试与验收

按仓库约束：API 集成测试（按需 Mock LLM）+ UI 自动化，不写 UT。

- Phase 0：旧快照术语列表/证据/review 回归；override 生效用例。
- Phase 1：候选来源可追溯（file/symbol/schema + source_version）100%；类型区分（Entity vs DTO、Command vs Event）。
- Phase 2：同源证据去重、冲突隔离、三类置信度阈值行为。
- Phase 3：硬约束阻止合并；合并/撤销事务；Grounding 四项失败路径；LLM Mock 输出契约。
- Phase 4：发布门禁、快照不可变、差异、增量幂等（重复提交同 commit 不产生重复记录）。
- UI：Terms 页证据/定义/发布流程；中英切换；空态与错误态。

质量指标按设计文档 15.2 分层统计；MVP 验收目标见设计文档 15.3。

---

## 8. 风险与取舍

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 范围过大（9 模块） | 8 周难以全部交付 | 严格按 Gate 推进；Phase 1/2 先形成可用闭环，Phase 3/4 可分期 |
| SQLite 并发与全文检索能力有限 | 大规模多仓性能不足 | 仓储接口隔离；FTS5 先用；规模化再迁 PG |
| CodeGraph 覆盖不足（DB/消息/ORM） | 部分候选来源缺失 | 以冻结源码补充解析；只对缺口语言接 Tree-sitter |
| 规则词表硬编码 | 中文/非英语命名识别差 | 规则外置配置；后续按标注集校准 |
| LLM 幻觉 | 定义不可信 | Evidence Package + 四项 Grounding + 仅草案状态 |
| 错误合并 | 知识身份破坏 | 硬约束优先、人工审核、补偿式回滚 |
| 旧数据兼容 | 升级后旧快照不可读 | 新列可空、类型映射、迁移测试 |

---

## 9. 待确认决策

1. **存储路线**：MVP 继续 SQLite（推荐），还是本期直接迁移 PostgreSQL + pgvector？
2. **首期范围**：先完成 Phase 0–2（证据与置信度闭环，推荐），还是一次性推进 Phase 0–4？
3. **术语类型兼容**：新类型体系上线后，旧 `resource/action/page` 值是否在 API 层直接映射为新类型（推荐），还是保留双写/双读一段时间？

确认后从 Phase 0 开始实施；每个功能完成即提交并重启服务。
