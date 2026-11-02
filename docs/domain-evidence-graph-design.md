# CodeGraph + Domain Evidence Graph：业务领域术语识别与词典构建方案

> 版本：V1.1 Final
> 状态：技术设计基线（待评审）
> 日期：2026-11-03
> 定位：本文是术语识别与业务词典建设的长期目标基线，取代并扩展 `docs/term-recognition-design.md` 的单仓规则版方案。
> 实施约束：本文保留原始架构设计；落地时必须结合当前工程栈（Python + SQLite + FastAPI + Vue）裁剪，差距与执行计划见 `docs/domain-evidence-graph-execution-plan.md`。

---

## 1. 方案定位与设计目标

### 1.1 建设目标

基于现有 CodeGraph，构建 Domain Evidence Graph（领域证据图谱），通过代码结构、数据库 Schema、API 契约、消息事件和服务依赖等多源信息，识别业务领域术语，建立标准化业务语义模型，最终形成可追溯、可审核、可发布、可持续演进的企业业务词典。

系统需要解决六个核心问题：

1. 术语发现：从不同语言、模块和微服务中识别潜在业务术语。
2. 语义识别：理解术语在特定业务上下文中的真实含义。
3. 证据验证：通过多源证据交叉验证术语及其定义。
4. 概念归一化：识别同义词、缩写、历史名称及跨服务概念映射。
5. 领域建模：识别 Bounded Context 和业务概念之间的关系。
6. 知识治理：实现术语审核、版本发布、变更追踪和回滚。

最终交付物不是一个简单的术语列表，而是一套有证据支撑的业务知识体系。

### 1.2 核心设计原则

| 原则 | 设计要求 |
| --- | --- |
| Evidence First | 所有业务定义必须能够追溯到原始证据 |
| Multi-source Verification | 多源证据交叉验证，不依赖单一代码命名 |
| Context Isolation | 相同名称在不同 Bounded Context 中允许具有不同含义 |
| LLM as Reasoner | LLM 负责语义推断，不负责事实裁决 |
| Human-in-the-loop | 关键归一化和正式发布经过人工审核 |
| Confidence Calibration | 置信度依据独立标注数据校准 |
| Reversible Evolution | 合并、拆分、变更和发布均可追溯、可恢复 |
| Incremental Processing | 支持代码变更后的增量分析 |
| Immutable Release | 正式发布的词典版本不可变 |

### 1.3 系统边界

系统采用职责分离：

- CodeGraph：提供代码结构事实和代码依赖关系。
- Domain Evidence Graph：组织业务术语、证据、上下文及领域关系。
- Semantic Engine：基于证据进行业务语义推断。
- Governance Engine：负责人工审核、正式确认和发布。
- Glossary：保存经过确认的正式业务知识。

核心原则是：CodeGraph 不承担业务语义裁决，LLM 不承担事实裁决，未经治理审核的推断不进入正式词典。

## 2. 总体架构

### 2.1 九模块架构

输入资产层：

Git Repository · CodeGraph · DB Schema · API · Proto · Message Contract · Documentation

1. Asset Discovery — 资产发现与服务上下文登记
2. Candidate Mining — 候选术语抽取
3. Evidence Fusion — 证据融合、去重、冲突仲裁
4. Confidence Calibration — 置信度计算与校准
5. Concept Normalization — 概念识别、同义归一化、合并与回滚
6. Semantic Analysis — LLM 定义生成与 Grounding
7. Domain Graph Construction — 领域关系和上下文建模
8. Knowledge Governance — 审核、版本、发布与审计
9. Continuous Evolution — 增量分析、质量评估、变更影响和知识演进

### 2.2 模块职责与输入输出

| 模块 | 核心职责 | 主要输出 |
| --- | --- | --- |
| 01 Asset Discovery | 仓库、服务、模块、语言、契约发现 | 资产清单、初始上下文 |
| 02 Candidate Mining | 从代码和数据契约抽取术语候选 | CandidateTerm |
| 03 Evidence Fusion | 证据采集、独立性判断、冲突处理 | Evidence Package |
| 04 Confidence Calibration | 计算并校准不同类型置信度 | Calibrated Confidence |
| 05 Concept Normalization | 同义判断、概念聚类、合并和回滚 | Canonical Concept |
| 06 Semantic Analysis | 业务定义、属性、动作和事件推断 | Definition Draft |
| 07 Domain Graph Construction | 领域关系、上下文和跨服务建模 | Domain Evidence Graph |
| 08 Knowledge Governance | 人工审核、发布、审计和版本管理 | Official Glossary |
| 09 Continuous Evolution | 增量处理、差异分析、质量监控 | Evolution Records |

九个模块采用统一的数据契约，不要求一开始部署为九个独立服务。MVP 推荐模块化单体架构，按职责分层，后续根据规模演进。

## 3. 标准术语模型

### 3.1 术语分类

采用四类核心业务术语，以及必要的辅助类型。

| 类型 | 业务含义 | 示例 |
| --- | --- | --- |
| ENTITY | 具有业务身份或生命周期的对象 | Order、Customer、Account |
| BUSINESS_EVENT | 已经发生或被发布的业务事实 | OrderPaid、AccountFrozen |
| BUSINESS_ACTION | 业务主体执行的业务操作 | CreateOrder、RefundPayment |
| BUSINESS_ATTRIBUTE | 描述业务对象的属性 | OrderAmount、PaymentStatus |
| BUSINESS_ENUM | 具有业务含义的枚举及枚举值 | OrderStatus、PAID |
| COMMAND | 要求系统执行某项业务操作的请求 | SubmitOrder |
| API_RESOURCE | 对外暴露的业务资源 | /orders、/accounts |
| DTO | 数据传输结构 | CreateOrderRequest |
| TECHNICAL_COMPONENT | 技术实现组件 | Repository、Adapter |

必须区分：

- Entity 与 DTO。
- Command 与 Event。
- 业务动作与技术方法。
- 业务属性与普通变量。
- API Resource 与领域实体。

例如，`CreateOrderCommand` 表达执行创建订单的请求，`OrderCreatedEvent` 表达订单创建这一事实已经发生，两者不能合并为同一种术语。

### 3.2 术语标准属性

| 字段 | 含义 |
| --- | --- |
| term_id | 全局唯一标识 |
| canonical_name | 标准英文名称 |
| display_name | 展示名称 |
| term_type | 术语类型 |
| definition | 业务定义 |
| domain_id | 所属业务领域 |
| bounded_context_id | 所属业务上下文 |
| aliases | 同义词、缩写、历史名称 |
| source_refs | 代码及契约引用 |
| evidence_refs | 支持和反对证据 |
| source_of_truth | 权威业务数据来源 |
| term_confidence | 术语成立置信度 |
| definition_confidence | 定义可信度 |
| owner | 业务 Owner |
| steward | 术语管理员 |
| lifecycle_status | 术语生命周期 |
| version | 术语版本 |
| effective_time | 生效时间 |
| deprecated_reason | 废弃原因 |
| replaced_by | 替代术语 |

`source_of_truth` 应按具体业务概念或属性确定，而不是简单指定某个微服务为全局权威源。

### 3.3 术语生命周期

| 状态 | 含义 |
| --- | --- |
| DISCOVERED | 已发现候选术语 |
| EVIDENCE_COLLECTED | 已采集证据 |
| ANALYZED | 已完成语义分析 |
| REVIEW_PENDING | 等待审核 |
| APPROVED | 已通过审核 |
| PUBLISHED | 已正式发布 |
| DEPRECATED | 已废弃 |
| REJECTED | 已拒绝 |

正式发布状态与分析任务状态分离。重新分析不能直接覆盖已发布的正式定义。

## 4. Domain Evidence Graph 数据模型

### 4.1 设计目标

CodeGraph 表达代码结构事实，Domain Evidence Graph 表达业务概念及其证据。

二者通过稳定的仓库、Commit、Symbol 和 Source Locator 关联。

### 4.2 图谱节点

| 节点 | 含义 |
| --- | --- |
| Repository | 代码仓库 |
| Service | 微服务 |
| Module | 业务或技术模块 |
| BoundedContext | 业务上下文 |
| CandidateTerm | 候选术语 |
| CanonicalTerm | 标准术语 |
| CodeSymbol | 代码符号 |
| DataSchema | 数据库 Schema |
| DataTable | 数据表 |
| DataField | 数据字段 |
| APIEndpoint | API 接口 |
| MessageContract | 消息契约 |
| Evidence | 证据 |
| Definition | 术语定义 |
| ReviewRecord | 审核记录 |
| TermVersion | 术语版本 |

### 4.3 图谱关系

代码关系：

| 关系 | 含义 |
| --- | --- |
| CALLS | 调用 |
| REFERENCES | 引用 |
| IMPLEMENTS | 实现 |
| EXTENDS | 继承 |
| DEPENDS_ON | 技术依赖 |

数据关系：

| 关系 | 含义 |
| --- | --- |
| MAPS_TO | 映射 |
| PERSISTS_IN | 持久化 |
| READS_FROM | 读取 |
| WRITES_TO | 写入 |
| SERIALIZES_AS | 序列化为 |

业务关系：

| 关系 | 含义 |
| --- | --- |
| HAS_ATTRIBUTE | 拥有业务属性 |
| PERFORMS | 执行业务动作 |
| TRIGGERS | 触发业务事件 |
| CONSUMES | 消费业务事件 |
| PART_OF | 属于 |
| ALIAS_OF | 别名关系 |
| SPECIALIZES | 特化 |
| AGGREGATES | 聚合 |
| REFERENCES_BUSINESS | 业务引用 |
| SAME_CONCEPT_AS | 跨上下文概念对应 |

业务关系和技术关系分别存储，不允许仅根据代码调用关系直接推导业务关系。

### 4.4 统一证据模型

Evidence 是系统的基础数据对象。

| 字段 | 含义 |
| --- | --- |
| evidence_id | 证据唯一标识 |
| evidence_type | 证据类型 |
| source_id | 来源资产 ID |
| source_locator | 文件、Symbol、Schema 或契约定位 |
| source_version | 来源版本 |
| subject_id | 证据主体 |
| predicate | 证据表达的关系或属性 |
| object_id/value | 证据对象或值 |
| polarity | SUPPORT / AGAINST / NEUTRAL |
| reliability | 来源可靠性 |
| independence_group | 独立性分组 |
| content_hash | 证据内容指纹 |
| derived_from | 直接派生来源 |
| observed_at | 采集时间 |
| valid_from / valid_to | 业务有效时间 |

证据必须保留原始来源、版本和定位信息。

### 4.5 证据独立性最小实现

V1.1 不建设完整证据依赖图，仅使用四项信息：

- `independence_group`：独立证据组。
- `content_hash`：内容去重指纹。
- `source_version`：来源版本。
- `derived_from`：直接派生来源。

判定规则：

1. 相同内容指纹的证据优先去重。
2. 自动生成或复制的内容保留派生关系。
3. 同一原始定义派生的证据属于同一个独立性分组。
4. 不同来源只有在不存在明确派生关系时，才可以作为独立证据候选。
5. 无法确认独立性的证据不重复计权。

`derived_from` 第一阶段只记录直接来源，不做递归依赖图分析。`independence_group` 由来源依赖关系和分析规则共同确定，不简单等同于 Repository 或 Service。

## 5. 模块 01：Asset Discovery

### 5.1 资产发现范围

| 资产 | 主要信息 |
| --- | --- |
| Git Repository | 仓库、分支、Commit |
| Service | 服务名称、职责、部署边界 |
| Module | 模块结构和依赖 |
| Programming Language | Java、Go、TypeScript、Python 等 |
| Database | 数据库、Schema |
| API | REST、OpenAPI、GraphQL |
| RPC | gRPC、Proto |
| Messaging | Kafka、RabbitMQ |
| ORM | Entity、Mapping、Repository |
| Documentation | README、ADR、领域文档 |

### 5.2 Bounded Context 识别

微服务不等于 Bounded Context。

初始上下文识别综合以下证据：

1. 数据所有权。
2. API 和 RPC 契约边界。
3. 业务事件流。
4. 业务模型和状态机。
5. 服务依赖。
6. 团队职责和业务 Owner 信息。

系统输出候选上下文、边界证据和待确认问题。正式上下文归属由领域专家确认。

## 6. 模块 02：Candidate Mining

### 6.1 候选术语来源

| 来源 | 发现对象 | 示例 |
| --- | --- | --- |
| Class / Struct / Interface | 实体、值对象、领域模型 | Order、Money |
| Enum / Enum Member | 业务状态、类型 | OrderStatus、PAID |
| DTO / Request / Response | 业务输入输出 | CreateOrderRequest |
| ORM Mapping | 持久化业务对象 | OrderEntity |
| DB Table / Column | 业务数据概念 | orders、payment_status |
| Event / Message | 业务事件 | OrderPaid |
| Command | 业务操作请求 | SubmitOrder |
| API Resource | 对外业务资源 | /orders |
| Public Method | 公开业务操作 | cancelOrder |
| Service Method | 服务层业务行为 | calculateRefund |
| Event Handler | 事件处理行为 | onOrderPaid |
| Repository Operation | 数据访问行为 | findByOrderId |

### 6.2 候选优先级

| 优先级 | 来源 | 处理策略 |
| --- | --- | --- |
| P0 | Entity、Aggregate、Value Object、API Request/Response、Event、Command、DB Schema | 优先分析 |
| P1 | Enum、Enum Member、Service Method、API Resource | 次优先分析 |
| P2 | 普通 Variable、Parameter、Local Method | 辅助分析 |

优先级仅代表分析价值，不代表置信度。

### 6.3 抽取策略

采用确定性解析优先：

- AST 和 Symbol 提取。
- 类型定义与引用关系。
- ORM Entity 与 DB Schema 映射。
- API Resource、Operation、Request、Response。
- 消息 Topic、Producer、Consumer 和 Payload。
- 命名模式与上下文信息。

跨语言统一映射到标准术语类型，同时保留原始名称、语言、仓库、文件和 Symbol 定位。

### 6.4 输出

每个 CandidateTerm 包含：

- 原始名称和标准化名称。
- 候选类型。
- 仓库、服务、模块。
- 编程语言。
- Symbol 或 Schema 定位。
- 引用和调用关系。
- 候选优先级。
- 抽取规则及版本。

## 7. 模块 03：Evidence Fusion

### 7.1 证据分类

| 类别 | 来源 | 主要支持内容 |
| --- | --- | --- |
| Structural | Class、Struct、Enum | 结构化对象存在 |
| Identity | Primary Key、Unique Key | 业务身份 |
| Persistence | ORM、DB Schema | 持久化关系 |
| Contract | API、Proto、OpenAPI | 对外契约 |
| Event | Kafka、RabbitMQ、Domain Event | 业务事件 |
| Behavior | Service Method、Handler | 业务行为 |
| Lifecycle | 状态机、状态转换 | 对象生命周期 |
| Ownership | 数据写入服务、团队归属 | 数据权威来源 |
| Documentation | README、ADR、领域文档 | 业务描述 |

### 7.2 证据可靠性

证据可靠性按 `Term Type × Evidence Type × Context` 进行评估，而不是给所有证据配置固定权重。

| 术语类型 | 强证据 | 辅助证据 |
| --- | --- | --- |
| ENTITY | 主键、生命周期、数据所有权 | Class 名称、注释 |
| BUSINESS_EVENT | 消息契约、事件发布与消费 | Event Class 命名 |
| BUSINESS_ACTION | 业务入口、规则、状态变更 | Method 名称 |
| BUSINESS_ATTRIBUTE | Schema、业务映射、规则使用 | 普通变量名 |

初始权重采用专家规则，后续根据人工标注结果进行校准。

### 7.3 冲突分类

| 冲突类型 | 典型情况 | 处理策略 |
| --- | --- | --- |
| 同名异义 | 不同上下文中的 Customer | 上下文隔离 |
| 映射错误 | API Customer 映射到内部 Account | 保留映射冲突 |
| 版本漂移 | Schema 与代码不一致 | 按来源版本区分 |
| 权威源冲突 | 多个服务声称拥有同一业务数据 | Owner 仲裁 |
| 共享字段滥用 | 不同服务共享 status 字段 | 不按字段名合并 |
| 事实矛盾 | 文档描述与代码行为冲突 | 标记冲突，等待确认 |

### 7.4 冲突仲裁流程

1. 确认是否属于同一 Bounded Context。
2. 确认来源版本是否兼容。
3. 确认是否属于同源派生证据。
4. 确认业务数据所有权。
5. 检查契约、生命周期和状态机等强证据。
6. 无法确定时隔离冲突，不强制选择。

仲裁必须区分代码事实、业务事实和语义推断，不能以 LLM 判断替代业务 Owner 的权威确认。

### 7.5 Evidence Package

每个待分析候选生成统一 Evidence Package，包含：

- 候选术语信息。
- 所属仓库、服务和上下文。
- 支持证据。
- 反对证据。
- 证据独立性信息。
- 证据可靠性。
- 冲突清单。
- 来源版本。
- 原始 Source Locator。

后续 Semantic Analysis 和 Concept Normalization 必须使用该标准化证据包，不允许绕过证据层直接将整个代码仓库交给 LLM 自由定义业务语义。

## 8. 模块 04：Confidence Calibration

### 8.1 三种独立置信度

| 指标 | 含义 |
| --- | --- |
| Term Confidence | 候选是否为有效业务术语 |
| Definition Confidence | 业务定义是否有充分证据支持 |
| Relation Confidence | 两个术语之间的业务关系是否成立 |

三种置信度独立计算，不使用单一 confidence 代替所有判断。

### 8.2 计算因素

- 支持证据质量。
- 反对证据质量。
- 证据独立性。
- 来源可靠性。
- 上下文一致性。
- 生命周期和数据所有权。
- 历史审核结果。

抽象模型：

C_term = f(E_support, E_against, I, R, T)

其中，支持和反对证据必须按独立性分组计算，避免重复证据虚增分数。

Definition Confidence 和 Relation Confidence 使用各自对应的证据与评估模型。

### 8.3 置信度校准

建立独立人工标注集：

- 人工标注正确与错误样本。
- 按术语类型和业务上下文分层。
- 通过验证集校准规则或模型输出。
- 使用 ECE、Brier Score、分桶准确率评估。

可使用 Platt Scaling 或 Isotonic Regression 进行概率校准。

LLM 自报 confidence 不能作为最终置信度。

### 8.4 决策阈值

初始阈值如下，后续根据标注集校准：

| 置信度 | 处理 |
| --- | --- |
| ≥ 0.90 | 允许自动生成下一阶段建议 |
| 0.70–0.90 | 重点人工复核 |
| 0.40–0.70 | 补充证据 |
| 低于 0.40 | 降低优先级或标记为噪声 |

硬冲突优先于分数：存在硬约束冲突时，无论置信度多高，都不能自动合并或发布。

## 9. 模块 05：Concept Normalization

### 9.1 目标

识别跨服务、跨语言的同义词、缩写、历史名称以及业务概念映射。

### 9.2 判断维度

- 名称和语义相似度。
- 主键和唯一标识。
- API Resource 和契约。
- 生命周期。
- 状态机。
- 业务行为。
- 数据所有权。
- Bounded Context。
- 业务文档。

Embedding 仅用于候选召回和相似度排序，不作为最终合并依据。

### 9.3 三类约束

Hard Constraint：禁止自动合并

- 已确认属于不同 Bounded Context，且缺少明确的概念映射证据。
- 业务身份或生命周期存在无法解释的根本冲突。
- 业务 Owner 已确认是不同概念。
- 来源版本不兼容，无法建立可靠映射。

Soft Constraint：影响匹配分数

- 名称相似。
- 字段结构相似。
- API Resource 相似。
- 业务枚举相似。
- 引用关系接近。

Review Constraint：必须人工复核

- 不同服务使用相同主键映射到不同业务模型。
- 多个服务都声称拥有同一业务对象。
- 生命周期或状态机不同。
- 跨语言映射缺少结构化证据。

### 9.4 成对判断

对候选术语进行 Pairwise Matching，输出以下结果：

| 结果 | 含义 |
| --- | --- |
| SAME_CONCEPT | 同一业务概念 |
| ALIAS | 同义词或别名 |
| CONTEXT_PROJECTION | 不同上下文中的概念投影 |
| SPECIALIZATION | 泛化与特化 |
| RELATED | 存在关联但不是同一概念 |
| DISTINCT | 不同概念 |
| UNCERTAIN | 证据不足 |

每次判断必须保存：

- 两个候选术语 ID。
- 判断结果。
- 支持证据。
- 反对证据。
- 判断置信度。
- 算法及模型版本。
- 审核状态。

### 9.5 合并操作与回滚

术语合并是知识身份变更，必须记录为独立的业务操作。

新增 `merge_operation`，包含：

| 字段 | 含义 |
| --- | --- |
| operation_id | 合并操作 ID |
| source_term_ids | 被合并术语 ID |
| target_term_id | 合并目标术语 ID |
| reason | 合并原因 |
| evidence_refs | 决策证据 |
| reviewer_id | 审核人 |
| created_at | 操作时间 |
| affected_relations | 受影响关系 |
| status | 操作状态 |
| reversed_by | 对应撤销操作 ID |

合并流程：

1. 完成成对判断。
2. 检查 Hard Constraint。
3. 检查证据冲突。
4. 生成合并建议。
5. 人工审核。
6. 在单个数据库事务中完成术语状态更新、关系迁移和合并记录写入。
7. 保留原始术语和历史关系记录。

回滚采用补偿操作：

- 不删除原始合并记录。
- 创建新的 REVERSE_MERGE 操作。
- 恢复原术语状态。
- 恢复或重建受影响关系。
- 记录撤销原因、审核人和时间。

正式词典历史版本通过发布快照保持不变，不因当前术语合并而被覆盖。

## 10. 模块 06：Semantic Analysis

### 10.1 定义生成

基于 Evidence Package 生成标准化业务定义。

定义至少包含：

1. Canonical Name。
2. Business Definition。
3. Term Type。
4. Business Context。
5. Attributes。
6. Lifecycle。
7. Operations。
8. Events。
9. Relationships。
10. Evidence References。
11. Unknowns。

### 10.2 FACT、INFERENCE、UNKNOWN

| 类型 | 含义 | 要求 |
| --- | --- | --- |
| FACT | 证据直接支持的事实 | 必须绑定 evidence_id |
| INFERENCE | 基于证据推导的结论 | 必须提供支持证据 |
| UNKNOWN | 当前证据不足以确定 | 保留待确认问题 |

示例：

术语：Order

- FACT：存在订单 ID、状态字段和持久化映射。
- INFERENCE：Order 很可能是具有生命周期的业务实体。
- UNKNOWN：是否支持部分取消，当前证据不足。

### 10.3 LLM 行为约束

- 只能基于输入证据推断。
- 不得用行业常识补充未经证实的事实。
- 证据不足时输出 UNKNOWN。
- 区分 ENTITY、ATTRIBUTE、ACTION、EVENT、COMMAND。
- 识别同名异义和同义异名。
- 不得将技术实现直接包装为业务定义。
- 不得自行执行正式术语合并。
- 不得修改 `source_of_truth`。
- 不得发布正式词典。

### 10.4 Grounding 最小校验

V1.1 采用四项程序化校验，不建设完整 Claim-level Grounding 系统。

| 校验 | 规则 |
| --- | --- |
| Evidence Existence | FACT 必须绑定真实存在的 evidence_id |
| Version Consistency | 证据来源版本必须与当前分析输入版本兼容 |
| Locator Validity | source_locator 必须能够解析到对应资产 |
| Contradiction Check | 检查与定义相关的反对证据 |

版本校验需要同时考虑源版本和分析版本：证据必须属于当前分析输入快照，分析任务必须记录所使用的 `analysis_version`。

反对证据存在并不意味着定义一定错误，但未解决的实质性冲突必须阻止正式发布。

校验失败时，定义只能保留为待修订草案，不能进入正式审核通过状态。

### 10.5 LLM 可复现与安全

每次分析记录：

- Model Name / Version。
- Prompt Version。
- Evidence Package Version。
- Analysis Version。
- Input Hash。
- Generation Timestamp。
- Structured Output。
- Grounding Result。

发送模型前执行敏感信息识别、脱敏和权限检查。密钥、个人信息及未授权的生产数据不得进入模型输入。

## 11. 模块 07：Domain Graph Construction

### 11.1 领域关系构建

关系发现分为三层：

1. 代码关系：来自 CodeGraph。
2. 数据关系：来自 DB、ORM、API 和消息契约。
3. 业务关系：基于证据和语义推断。

示例：

- Order HAS_ATTRIBUTE OrderAmount。
- Order PERFORMS CancelOrder。
- OrderPaid TRIGGERS UpdateOrderStatus。
- Order CONSUMES PaymentSucceeded。

业务关系不能直接由技术调用关系替代。

### 11.2 关系属性

每条关系至少包含：

- relation_id。
- source_term_id。
- target_term_id。
- relation_type。
- direction。
- cardinality。
- evidence_refs。
- relation_confidence。
- bounded_context_id。
- review_status。
- effective_time。
- version。

### 11.3 Bounded Context 隔离

每个正式术语必须归属明确的 Bounded Context，或明确标记为待确认。

不同上下文中的同名术语允许独立存在。

例如：

- Order Management Context 中的 Order。
- Fulfillment Context 中的 Order。
- Billing Context 中的 Order。

它们可能是同一业务对象的不同投影，也可能是不同概念。

系统通过 `SAME_CONCEPT_AS`、`CONTEXT_PROJECTION`、`REFERENCES_BUSINESS` 等关系表达概念对应，不能仅凭名称相同而合并。

### 11.4 跨微服务关联

| 技术机制 | 识别依据 |
| --- | --- |
| REST / OpenAPI | Resource、Operation、Request、Response |
| gRPC / Proto | Service、RPC、Message |
| Kafka | Topic、Producer、Consumer、Payload |
| RabbitMQ | Exchange、Queue、Routing Key |
| Shared Database | Schema、Table、Column |
| Event-driven | 发布、订阅和事件处理链路 |
| Distributed Transaction | Saga、补偿操作、事务状态 |

必须区分数据所有权、API 依赖、事件订阅、业务引用和概念投影。

## 12. 模块 08：Knowledge Governance

### 12.1 角色模型

| 角色 | 职责 |
| --- | --- |
| Domain Expert | 确认业务定义和语义 |
| Business Owner | 确认业务概念和权威数据源 |
| Terminology Steward | 管理术语名称、类型和别名 |
| Reviewer | 审核定义、关系和归一化 |
| Platform Admin | 管理数据源、权限和平台配置 |

### 12.2 审核流程

1. 系统生成候选术语和 Evidence Package。
2. 系统完成证据冲突和 Grounding 检查。
3. Terminology Steward 审核名称、类型、别名和上下文。
4. Domain Expert 审核业务定义、属性和关系。
5. Business Owner 确认关键概念及权威来源。
6. 审核通过后生成正式词典发布快照。

审核记录至少包含审核人、审核意见、审核时间、审核对象版本和决策结果。

### 12.3 发布门禁

正式发布前必须满足：

- FACT 均绑定有效证据。
- 证据版本与分析输入一致。
- Source Locator 可以解析。
- 不存在未解决的关键证据冲突。
- Bounded Context 已明确或明确标记待确认。
- 关键术语已完成业务 Owner 审核。
- 术语合并有完整审核记录。
- 发布内容不包含未经授权的敏感数据。

### 12.4 不可变发布快照

正式词典发布不能依赖当前术语表动态查询历史状态，必须生成不可变快照。

新增两张核心表：

`release_snapshot`：

| 字段 | 含义 |
| --- | --- |
| release_id | 发布唯一 ID |
| release_name | 发布名称 |
| version | 词典版本 |
| status | 发布状态 |
| created_by | 发布人 |
| created_at | 发布时间 |
| description | 发布说明 |
| snapshot_hash | 整体快照指纹 |

`release_item`：

| 字段 | 含义 |
| --- | --- |
| release_id | 发布版本 ID |
| item_type | TERM / RELATION |
| item_id | 术语或关系 ID |
| item_version | 对应对象版本 |
| snapshot_content | 发布时的完整内容快照 |
| content_hash | 单项内容指纹 |

发布快照至少包含：

- 正式术语名称和定义。
- 术语类型和上下文。
- 别名和权威来源。
- 正式业务关系。
- 术语及关系版本。
- 必要的证据引用和审核信息。

发布流程：

1. 选定待发布的正式术语和关系。
2. 读取确定版本的术语及关系内容。
3. 生成完整快照和内容指纹。
4. 写入 `release_snapshot` 和 `release_item`。
5. 在同一个数据库事务中完成发布状态变更。
6. 发布完成后禁止修改快照内容。

发布回滚不是删除或覆盖历史 Release，而是创建新的发布版本，以历史快照为基础重新发布。这样可以保留完整的发布历史和差异记录。

## 13. 模块 09：Continuous Evolution

### 13.1 增量分析流程

以 Git Commit 为增量触发单位：

1. 识别新增、修改和删除文件。
2. 定位受影响的 Code Symbol。
3. 找到关联候选术语和证据。
4. 识别受影响的领域关系。
5. 重新执行必要的证据融合和置信度计算。
6. 重新分析受影响的定义。
7. 生成词典变更建议。
8. 经审核后发布新版本。

### 13.2 增量幂等

采用以下幂等键：

K = H(repo, commit, analysisVersion, inputHash)

其中：

- `repo`：仓库唯一标识。
- `commit`：代码提交版本。
- `analysisVersion`：分析规则、模型和 Prompt 版本的组合标识。
- `inputHash`：本次实际分析输入的整体内容指纹。

这里明确区分：

- `content_hash`：单个证据或资产的内容指纹。
- `input_hash`：本次分析输入的整体指纹。

幂等键通过数据库唯一约束保证，同一输入重复执行时复用已有成功结果，不重复产生术语、证据和定义记录。

失败任务可以重试；只有成功完成的分析结果才标记为可复用。分析任务状态和结果提交必须具有明确的事务边界，避免任务中断后出现不完整的成功记录。

V1.1 不引入 Outbox Pattern，不建立额外的分布式消息一致性体系。

### 13.3 版本体系

统一使用四类版本：

| 版本 | 含义 |
| --- | --- |
| source_version | 代码、Schema、API 和消息契约来源版本 |
| analysis_version | 分析规则、模型、Prompt 和算法版本 |
| term_version | 术语自身版本 |
| release_version | 正式词典发布版本 |

源代码变更不等于业务术语变更。只有经过审核确认的业务知识变化，才产生正式词典版本变化。

## 14. 技术实现与存储方案

### 14.1 推荐技术栈

| 层次 | 技术 | 职责 |
| --- | --- | --- |
| Code Analysis | CodeGraph | 代码结构与依赖分析 |
| Multi-language Parsing | Tree-sitter | 多语言 AST 解析 |
| Backend | TypeScript / Node.js | 业务流程和领域服务 |
| Metadata Storage | PostgreSQL | 术语、证据、关系、版本、审核 |
| Vector Search | pgvector | 术语语义召回 |
| Full-text Search | PostgreSQL FTS | 关键词搜索 |
| Job Queue | BullMQ + Redis | 异步任务和重试 |
| Semantic Engine | LLM + Structured Output | 语义推断 |
| Embedding | Embedding Model | 相似度召回 |
| Frontend | React | 术语审核和图谱浏览 |

> 实施适配：当前工程实际为 Python + SQLite(WAL) + FastAPI + Vue，CodeGraph 已覆盖 30+ 语言。MVP 阶段建议沿用现有栈，不引入 Node/Postgres/Redis；存储层以可替换接口隔离，保留迁移 PostgreSQL + pgvector 的空间。详见执行计划。

### 14.2 存储决策

V1.1 采用 PostgreSQL + pgvector，不引入独立图数据库。

原因：

- 术语、证据和审核具有明显关系型数据特征。
- 事务和版本控制是当前核心需求。
- PostgreSQL 足以满足 MVP 的关系查询和有限图遍历。
- pgvector 可支持术语相似度召回。
- 可以降低基础设施和运维成本。

从第一天起抽象 Node、Edge、Evidence、Context、Version 等稳定模型，为未来迁移专用图数据库保留空间。

### 14.3 核心数据表

| 表 | 职责 |
| --- | --- |
| repository | 仓库资产 |
| service | 微服务 |
| module | 模块信息 |
| bounded_context | 业务上下文 |
| code_symbol | CodeGraph 符号索引 |
| candidate_term | 候选术语 |
| canonical_term | 标准术语 |
| term_alias | 术语别名 |
| evidence | 原始证据 |
| evidence_package | 证据包 |
| evidence_conflict | 证据冲突 |
| term_pair_judgment | 成对归一化判断 |
| term_relation | 术语关系 |
| term_definition | 术语定义 |
| term_version | 术语版本 |
| merge_operation | 合并与回滚操作 |
| review_record | 审核记录 |
| release_snapshot | 发布快照 |
| release_item | 发布项 |
| analysis_run | 分析任务 |
| audit_log | 操作审计 |

关键对象采用稳定 ID、明确版本和来源关联。证据和发布快照采用不可变记录策略；需要更新时创建新记录或新版本。

### 14.4 CodeGraph 集成边界

CodeGraph 负责：

- 仓库解析。
- AST 和 Symbol 提取。
- 调用关系。
- 类型引用关系。
- 模块依赖关系。

Domain Evidence Graph 负责：

- 候选业务术语。
- 多源证据。
- 术语归一化。
- 业务定义。
- Bounded Context。
- 业务关系。
- 审核与发布。

通过适配层读取 CodeGraph，使用稳定的 Repository ID、Commit ID 和 Symbol ID 建立映射，不将业务术语模型直接侵入 CodeGraph 核心结构模型。

如果 CodeGraph 使用 SQLite，则 SQLite 作为代码分析侧存储，PostgreSQL 作为领域知识侧权威存储。通过 Commit、内容指纹和稳定 ID 实现幂等同步，不建立两个业务主库之间的双向写入。

### 14.5 LLM 成本控制

采用分层处理：

| 方式 | 适用场景 |
| --- | --- |
| 规则引擎 | 结构和映射提取 |
| Embedding | 候选召回和相似术语搜索 |
| 小模型 | 初步分类和简单判断 |
| 强模型 | 复杂定义和跨服务语义分析 |
| 人工审核 | 高风险冲突和正式确认 |

通过输入指纹缓存、证据去重、增量分析、P0 优先级和上下文控制降低成本。

## 15. 质量评估与验收标准

### 15.1 Ground Truth

建立由业务专家和熟悉业务代码的工程师参与的人工标注集，覆盖：

- 正确业务术语。
- 技术噪声。
- 同名异义。
- 同义异名。
- 跨上下文投影。
- 正确业务关系。
- 错误关系。
- 定义证据充分性。
- 权威数据来源。

关键样本双人独立标注，使用 Cohen's Kappa 衡量一致性，分歧由第三方仲裁。

### 15.2 质量指标

| 模块 | 指标 |
| --- | --- |
| Candidate Mining | Precision / Recall |
| Evidence Fusion | Evidence Coverage |
| Evidence Fusion | Conflict Detection Rate |
| Normalization | Pairwise Precision / Recall |
| Normalization | False Merge Rate |
| Semantic Analysis | Definition Accuracy |
| Grounding | Evidence Citation Validity |
| Confidence | ECE / Brier Score |
| Domain Graph | Relation Precision / Recall |
| Governance | Approval Rate |
| Evolution | Incremental Accuracy |

指标必须按术语类型、业务上下文、编程语言和证据类型分层评估，避免整体准确率掩盖局部问题。

### 15.3 MVP 验收目标

以下是项目目标，实际阈值需要结合人工标注集确认。

| 指标 | 目标 |
| --- | --- |
| P0 候选来源可追溯率 | 100% |
| FACT 证据引用有效率 | 100% |
| 正式发布审核覆盖率 | 100% |
| 高置信度术语建议 Precision | ≥ 90% |
| 归一化错误合并率 | ≤ 2% |
| 关键证据冲突检测率 | ≥ 90% |
| 正式术语定义人工采纳率 | ≥ 70% |
| 增量任务幂等性 | 100% |
| 已发布快照内容可验证率 | 100% |

错误合并率优先于合并召回率。MVP 阶段宁可保留 UNCERTAIN，也不应为追求自动化而强制合并。

## 16. MVP 实施计划

### 16.1 范围控制

| 维度 | MVP 范围 |
| --- | --- |
| 业务领域 | 1 个 |
| 微服务 | 2–3 个 |
| 编程语言 | 1–2 种 |
| 核心术语 | ENTITY、BUSINESS_EVENT、BUSINESS_ACTION、BUSINESS_ATTRIBUTE |
| 数据来源 | CodeGraph、DB Schema、API、消息契约 |
| 归一化 | 半自动，人工确认 |
| 语义定义 | LLM 生成，程序化 Grounding |
| 存储 | PostgreSQL + pgvector |
| 正式发布 | 人工审核、不可变快照 |
| 增量处理 | Git Commit 驱动、幂等执行 |

MVP 不建设完整证据依赖图、Claim-level Grounding、Outbox、自动跨上下文合并和独立图数据库。

### 16.2 八周实施计划

1. P1 · 第 1–2 周：资产发现与候选发现
   - 接入 CodeGraph。
   - 建立 Repository、Service、Module 模型。
   - 接入 AST、DB Schema、API 和消息契约。
   - 实现 P0 候选术语抽取。
   - 建立 Symbol 与 Source Locator 映射。
   - 交付物：资产清单、候选术语库、来源定位索引。
2. P2 · 第 3–4 周：证据融合与置信度
   - 建立 Evidence 标准模型。
   - 实现内容指纹和同源证据去重。
   - 实现独立性分组和直接派生关系。
   - 实现冲突分类和隔离。
   - 建立初始可靠性规则。
   - 建立人工标注集及置信度校准流程。
   - 交付物：Evidence Package、冲突报告、置信度结果。
3. P3 · 第 5–6 周：归一化与语义分析
   - 实现术语成对判断。
   - 实现归一化硬约束。
   - 实现合并操作记录和补偿式回滚。
   - 接入 LLM 生成业务定义。
   - 实现 FACT / INFERENCE / UNKNOWN。
   - 实现四项 Grounding 校验。
   - 交付物：标准术语候选、定义草案、归一化审核队列。
4. P4 · 第 7–8 周：治理与正式发布
   - 实现术语审核界面。
   - 实现 Owner、Steward 和 Reviewer 角色。
   - 实现术语和关系版本管理。
   - 实现不可变发布快照和发布项。
   - 实现词典差异比较。
   - 实现 Commit 增量分析和幂等控制。
   - 交付物：正式业务词典、领域图谱、审核发布流程、增量分析能力。

### 16.3 阶段验收 Gate

| Gate | 验收标准 | 未通过处理 |
| --- | --- | --- |
| G1：候选发现 | 候选术语可以追溯到仓库、Commit 和源位置 | 不进入语义分析 |
| G2：证据融合 | 同源证据去重，关键冲突能够识别和隔离 | 不进入归一化 |
| G3：概念归一化 | 硬约束有效，合并可审计、可回滚 | 不允许自动合并 |
| G4：定义 Grounding | FACT 有效引用证据，版本和定位校验通过 | 不进入正式审核 |
| G5：知识治理 | Owner 审核完成，发布快照不可变且可验证 | 不允许正式发布 |

## 17. 风险控制与非功能要求

### 17.1 主要风险

| 风险 | 应对策略 |
| --- | --- |
| 代码命名不规范 | 多源证据交叉验证 |
| 多语言语义差异 | 统一中间模型，保留原始语言 |
| 同名异义 | Bounded Context 隔离 |
| 同源证据重复 | 内容指纹、独立性分组 |
| 文档与代码冲突 | 版本区分、冲突隔离、Owner 仲裁 |
| LLM 幻觉 | Evidence Package 和 Grounding |
| 错误术语合并 | 硬约束、审核、补偿式回滚 |
| 增量重复执行 | 幂等键和数据库唯一约束 |
| 历史知识丢失 | 不可变版本和发布快照 |
| 敏感数据泄露 | 脱敏、访问控制、审计和模型权限限制 |

### 17.2 非功能要求

可靠性：

- 证据写入幂等。
- 合并和发布具有明确事务边界。
- 失败任务可重试。
- 正式发布不可变。
- 关键操作有审计记录。

可追溯性：任意正式术语应能够追溯到：

`Glossary Release → Term Version → Definition → Evidence Package → Evidence → Source Locator → Source Version`

可复现性：相同的源版本、分析版本和分析输入，应能够识别为相同分析任务。模型、Prompt 和规则发生变化时，必须产生新的分析版本。

安全性：

- 代码仓库访问遵循原有权限。
- 模型输入执行敏感信息检查。
- 术语审核和发布进行角色授权。
- 合并、回滚、发布保留操作记录。

可扩展性：

- 多语言解析器可插拔。
- 新证据来源通过适配层接入。
- 图谱存储与业务逻辑解耦。
- 术语模型支持新增类型和关系。

## 18. 最终架构决策清单

| 决策项 | 最终方案 |
| --- | --- |
| 基础代码分析 | 使用 CodeGraph |
| 多语言解析 | 使用 Tree-sitter 及对应解析适配器 |
| 业务知识模型 | 独立建设 Domain Evidence Graph |
| 术语抽取 | 结构化规则优先，多源补充 |
| 证据独立性 | 四字段最小实现，不建设完整依赖图 |
| 证据融合 | 支持去重、可靠性评估和冲突隔离 |
| 置信度 | Term、Definition、Relation 三类独立校准 |
| 术语归一化 | 成对判断、硬约束、人工审核 |
| 合并治理 | merge_operation + 补偿式回滚 |
| 语义分析 | LLM 基于 Evidence Package 生成 |
| Grounding | 四项程序化最小校验 |
| 领域建模 | Bounded Context 隔离，跨服务关系显式表达 |
| 图谱存储 | PostgreSQL + pgvector |
| 增量分析 | Commit + Analysis Version + Input Hash 幂等 |
| 正式发布 | release_snapshot + release_item，不可变 |
| 质量控制 | 人工标注集、分层指标、阶段 Gate |
| MVP 部署 | 模块化单体，不提前拆分微服务 |

## 19. 最终交付物

项目完成后应形成以下五类成果：

1. 标准业务词典：标准名称、业务定义、别名、术语类型、上下文、权威来源、版本及审核状态。
2. Domain Evidence Graph：业务术语、代码实现、数据库、API、事件、服务关系及证据之间的可追溯关联。
3. Bounded Context 模型：领域边界、上下文内术语定义、跨服务概念映射和上下文投影关系。
4. 知识治理体系：审核、合并、回滚、版本管理、发布快照、审计和变更影响分析。
5. 持续演进机制：代码变更触发增量分析，识别业务知识变化，经过审核后发布新的词典版本。

## 20. 最终结论

本方案的核心不是利用 LLM 从代码中生成一份业务术语表，而是建设一套基于代码事实、多源证据验证、语义推断和人工治理的业务知识生产系统。

其完整链路为：

`CodeGraph → Evidence → Normalization → Semantic Analysis → Domain Graph → Governed Glossary`

V1.1 的实施重点应严格限定为：

1. 先解决术语发现和证据可信度。
2. 再解决概念归一化和语义定义。
3. 然后建立领域关系和 Bounded Context。
4. 最后完成审核、不可变发布和增量演进。

架构上不增加不必要的系统复杂度，数据上保证可追溯，算法上保证证据约束，治理上保证操作可恢复。

这份方案可以作为技术设计评审、MVP 任务拆解和后续研发实施的统一基线。
