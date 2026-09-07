# CodeHistory 术语识别详细方案

> 状态：设计方案  
> 范围：单仓领域术语识别、跨仓术语对齐、自然语言查询中的术语定位  
> 依赖：CodeGraph SQLite（`nodes` / `edges` / `files`），LLM 为可选增强项

## 1. 背景与目标

CodeHistory 当前已经具备两类与“术语”相关的能力：

1. 从代码类型中识别核心实体；
2. 根据类型名称相似度对齐不同服务中的实体。

但当前输出仍以“代码实体列表”为主，并不等同于完整的业务术语体系。它缺少统一的候选生成、证据归档、同义词聚类、歧义处理和可解释的置信度模型。本方案在保留现有确定性算法的基础上，将术语识别建设成一条可审计、可增量、可配置的流水线。

目标如下：

- 从代码结构、命名、关系、注解和可选文本中发现业务术语；
- 区分领域实体、动作、状态、规则、事件和技术类型；
- 在单仓内合并同一概念的不同代码表达；
- 在多仓之间对齐相同或相关的业务概念；
- 每个结果都带来源位置、评分明细和判定理由；
- 无 LLM 时可稳定运行，启用 LLM 后只增强语义判断，不替代事实提取；
- 支持增量更新、人工校正和质量评估。

## 2. 术语与边界

本方案使用以下规范用语，避免把不同阶段都称为“识别”。

| 术语 | 定义 | 示例 |
|---|---|---|
| 代码符号 | CodeGraph 中可定位的语法实体 | `OrderService`、`OrderStatus` |
| 术语候选 | 从一个或多个代码符号中抽取、尚未确认的业务表达 | `Order`、`Payment` |
| 领域术语 | 有足够业务证据、已通过评分门槛的规范概念 | “订单” |
| 术语提及 | 术语在源码中的一次可定位出现 | `src/domain/Order.java:12` |
| 规范名 | 用于展示和检索的首选名称 | `Order` |
| 别名 | 指向同一概念的其他名称 | `PurchaseOrder`、`OrderDTO` |
| 实体识别 | 判断某个类型是否代表领域对象 | `Order` 是实体，`OrderController` 不是 |
| 术语对齐 | 判断两个候选是否表示相同或相关概念 | `UserAccount` ↔ `Account` |
| 查询术语定位 | 从用户问题中找到要检索的名称 | “谁调用了 `createOrder`”中的 `createOrder` |

不纳入第一阶段的内容：

- 从产品文档自动建立完整企业本体；
- 自动翻译并强制统一中英文术语；
- 仅凭 LLM 常识创建代码中没有证据的术语；
- 自动修改源码中的类型或变量名称。

## 3. 当前实现基线

### 3.1 单仓核心实体

当前 `CoreEntityExtractor` 从 CodeGraph 读取类型节点，构建类型关系图，并计算：

```text
score = PageRank × 30
      + min(field_count, 20) × 0.8
      + min(relationship_count, 20) × 0.5
      + 路径加减分
      + 注解加分
      + 名称后缀减分
```

现有规则：

- `domain/model/entity/pojo` 路径：`+8`；
- `types` 路径：`+4`；
- `Entity/Document/Table/Aggregate` 注解：`+10`；
- `controller/service/config` 路径：`-6`；
- `Controller/Service/Repository/Config/Test` 名称后缀：`-8`；
- `public/node_modules/*.d.ts`：`-14`；
- 默认按分数取前 30 个。

该算法适合回答“哪些类型更像核心领域对象”，但没有绝对准入门槛，因此返回的是排序结果，不是严格的术语判定。

### 3.2 跨服务实体对齐

当前跨仓逻辑先收集非测试的 `class/struct/interface/enum/type_alias`，再进行两两服务比较：

1. 精确同名得分 `1.0`；
2. 按下划线和 CamelCase 分词；
3. 使用词集合 Jaccard 相似度；
4. 一方词集合包含另一方时，最低提升到 `0.7`；
5. 对 `Service/Svc`、`Repository/Repo`、`Request/Req` 等后缀做有限归一；
6. 得分不低于 `0.6` 才成为候选映射；
7. 每一对服务内使用贪心一对一匹配；
8. 使用 `--llm` 时，只验证分数不低于 `0.7` 的前 20 个映射。

当前实现注释中提到“缩写展开”和“编辑距离”，但算法实际上尚未实现通用缩写展开与编辑距离。后续文档和代码应保持一致。

### 3.3 查询术语定位

问答入口优先让 LLM 将问题转换成受限的只读操作；没有 LLM 或计划解析失败时，启发式逻辑会：

- 优先读取反引号中的名称；
- 否则提取英文/代码标识符并取最后一个；
- 中文问题则移除“查找、功能、历史、调用”等固定提示词；
- 根据“调用、事件、历史、统计”等意图选择查询操作。

这只是检索关键词提取，不应与领域术语识别共用评分或数据模型。

## 4. 总体识别流程

```text
CodeGraph SQLite + 可选源码文本 + 仓库配置
                    │
                    ▼
          1. 数据检查与候选采集
                    │
                    ▼
          2. 名称解析与规范化
                    │
                    ▼
          3. 特征提取与候选分类
                    │
                    ▼
          4. 领域性评分与准入
                    │
                    ▼
          5. 单仓去重、别名归并
                    │
                    ▼
          6. 跨仓候选召回与匹配
                    │
                    ▼
          7. 可选 LLM 语义复核
                    │
                    ▼
          8. 冲突处理、结果持久化
                    │
                    ▼
          9. CLI / API / Web / MCP 输出
```

流水线必须遵循两个原则：

- **事实与推断分离**：文件、行号、类型、字段、关系来自 CodeGraph；“是否为领域术语”和“两个术语是否相同”属于推断。
- **召回与裁决分离**：规则负责低成本召回候选，评分器或 LLM 负责裁决，避免对所有实体做全量 LLM 比较。

## 5. 阶段一：数据检查与候选采集

### 5.1 前置检查

执行识别前检查：

- `.codegraph/codegraph.db` 是否存在；
- `nodes/edges/files` 表是否完整；
- CodeGraph 索引是否落后于源码修改时间；
- 节点数量是否异常为零；
- 当前 CodeGraph 版本是否受支持。

失败时返回结构化诊断，不得静默返回“没有术语”。

### 5.2 候选来源

优先级从高到低：

| 来源 | CodeGraph 节点/关系 | 可产生的术语类型 |
|---|---|---|
| 类型声明 | class/interface/record/struct/type/enum | 实体、值对象、状态 |
| 字段与属性 | contains → field/property/enum_member | 属性、状态值 |
| 类型关系 | references/type_of/extends/implements | 领域关联证据 |
| API 契约 | route、handler、请求/响应类型 | 资源、命令、查询 |
| 事件与消息 | topic/queue、handler、message 类型 | 领域事件 |
| 数据持久化 | Entity/Table/Document 注解、表名 | 实体、聚合 |
| 函数名称 | function/method | 业务动作 |
| 注释与 docstring | 可选源码读取 | 定义、别名、业务解释 |

第一版继续以类型声明为主，逐步增加 API、事件、状态和动作候选，避免一次扩展导致噪声失控。

### 5.3 基础过滤

默认排除：

- 测试、生成代码、依赖目录和构建产物；
- 匿名类型、长度过短且无其他证据的名称；
- 纯框架基类、通用工具类和配置类；
- 用户配置中的 `exclude_paths` 与 `exclude_patterns`。

排除只影响候选准入，原始证据仍可用于解释其他术语的关系。

## 6. 阶段二：名称解析与规范化

每个候选保留原名，同时产生只用于匹配的规范化表示，禁止覆盖源码名称。

### 6.1 分词

支持：

- CamelCase：`PurchaseOrderItem` → `purchase/order/item`；
- snake_case：`purchase_order` → `purchase/order`；
- kebab-case：`order-created` → `order/created`；
- 连续大写：`HTTPOrderDTO` → `http/order/dto`；
- 数字边界：`AddressV2` → `address/v2`；
- Unicode 名称原样保留，并按语言能力选择可选分词器。

### 6.2 词形与后缀处理

规范化分为三层：

1. 大小写、分隔符和单复数归一；
2. 技术后缀剥离：`DTO/VO/PO/DAO/Impl/Service/Controller/Repository`；
3. 受控缩写展开：`acct → account`、`qty → quantity`、`repo → repository`。

缩写表由系统默认值与仓库配置合并。业务缩写必须来自人工配置或高置信证据，不能依赖模型猜测后直接写入规范词典。

示例：

```text
OrderDTO          → tokens=[order,dto],        stem=[order]
PurchaseOrderVO   → tokens=[purchase,order,vo], stem=[purchase,order]
UserAcct          → tokens=[user,acct],         expanded=[user,account]
```

## 7. 阶段三：特征提取与候选分类

为每个候选构建 `TermEvidence`，至少包含以下特征组。

### 7.1 结构特征

- PageRank；
- 入度、出度和总关系数；
- 字段/属性/枚举成员数量；
- 被 API 请求或响应引用次数；
- 被业务方法引用次数；
- 继承、实现和类型引用关系。

### 7.2 位置特征

- 是否位于 `domain/model/entity/aggregate/valueobject/event`；
- 是否位于 `controller/config/infrastructure/generated/test`；
- 所属模块和架构分层；
- 是否跨多个模块被引用。

### 7.3 声明特征

- 类型种类；
- 注解/装饰器；
- 父类和接口；
- 是否具有稳定标识字段；
- 是否具有状态字段、时间字段或领域值字段。

### 7.4 命名特征

- 技术后缀数量；
- 是否包含领域词根；
- 是否属于框架/通用停用词；
- 名称信息量和唯一性；
- 规范化后的 token 集合。

### 7.5 候选分类

首期分类规则：

| 分类 | 强证据 |
|---|---|
| `entity` | Entity/Table/Document 注解、领域目录、标识字段、多关系 |
| `value_object` | 不可变结构、值类型后缀、被实体字段引用 |
| `state` | enum/type + 状态字段或状态转换使用 |
| `event` | Event/Message 后缀、消息通道、事件处理器引用 |
| `action` | 入口点或方法动词 + 领域对象参数/返回值 |
| `technical` | Controller/Config/Client/Adapter/Repository 等技术职责 |
| `unknown` | 证据不足或冲突 |

分类与准入分开：一个候选可以被分类为 `technical`，但默认不进入领域术语清单。

## 8. 阶段四：领域性评分与准入

### 8.1 评分模型

第一版沿用当前确定性公式，并把各项拆成可展示的 `score_breakdown`：

```text
domain_score = graph_score
             + structure_score
             + location_score
             + annotation_score
             + usage_score
             - technical_penalty
             - noise_penalty
```

建议将总分归一到 `[0, 1]`，同时保留原始分数用于迁移比对。阈值分层：

| 区间 | 处理 |
|---|---|
| `[0.80, 1.00]` | 自动接纳为领域术语 |
| `[0.60, 0.80)` | 接纳但标记为需复核，或进入 LLM 复核 |
| `[0.40, 0.60)` | 仅保留为候选，不进入默认结果 |
| `[0.00, 0.40)` | 过滤，但保留诊断计数 |

阈值必须通过标注集校准，以上只是初始建议值。

### 8.2 强制规则

评分之外允许少量高精度规则：

- 明确的领域注解可提升至最低接纳区间；
- 生成代码、依赖代码和纯测试代码强制排除；
- 用户黑名单强制排除；
- 用户白名单强制接纳，但标记 `source=user_override`。

每次强制覆盖都必须写明规则 ID，保证结果可追溯。

## 9. 阶段五：单仓去重与别名归并

先用低成本 blocking 产生可能重复的候选对，避免 `O(n²)` 全量比较。

Blocking key 可包括：

- 相同规范词根；
- 相同末尾领域 token；
- 相同表名或序列化名称；
- 相同 API 资源名；
- 直接类型引用或转换关系。

候选对评分建议：

```text
alias_score = 0.35 × name_similarity
            + 0.20 × field_similarity
            + 0.15 × relation_similarity
            + 0.15 × api_context_similarity
            + 0.10 × persistence_similarity
            + 0.05 × module_proximity
```

关系只能是以下枚举之一：

- `same`：同一业务概念；
- `subset`：一方只表达另一方的一部分；
- `wrapper`：传输或适配包装；
- `related`：相关但不可合并；
- `false`：误匹配。

只有 `same` 才自动并入一个规范术语。`subset/wrapper/related` 保存为关系边，不能当作别名。

规范名选择顺序：用户指定名 > 领域层名称 > 持久化实体名称 > API 公共名称 > 使用频率最高且技术后缀最少的名称。

## 10. 阶段六：跨仓术语对齐

### 10.1 候选召回

对每两个逻辑服务，按以下通道召回候选对：

- 规范 token 精确或包含匹配；
- 受控缩写展开后匹配；
- API 路径资源名匹配；
- HTTP/MQ/gRPC 调用边两端的请求、响应或消息类型；
- 相同持久化表名或序列化字段集合；
- 可选向量相似度，仅作为召回手段。

至少命中一个 blocking 条件才进入精排。

### 10.2 匹配评分

跨仓匹配不能只依赖名称，建议采用：

```text
cross_repo_score = 0.35 × name_similarity
                 + 0.20 × field_similarity
                 + 0.15 × call_context_similarity
                 + 0.15 × api_or_message_similarity
                 + 0.10 × persistence_similarity
                 + 0.05 × description_similarity
```

字段相似度必须比较规范化字段名和类型族；调用上下文比较候选参与的入口点、消息主题和相邻服务。

### 10.3 分配策略

替换当前按遍历顺序的贪心匹配：

- 默认允许一对多，因为同一个企业概念可能在服务内拆成多个上下文模型；
- 若业务场景要求一对一，使用最大权重二分匹配；
- 所有超过阈值的备选关系都保留在诊断信息中；
- 同分冲突进入人工复核，不按字典顺序静默选择。

## 11. 阶段七：LLM 语义复核

LLM 只处理规则无法可靠裁决的候选对，不直接扫描整个仓库。

### 11.1 输入

提供最小、可验证的证据包：

- 两个候选的名称、分类和所在服务；
- 字段摘要；
- 注解、父类型和关键关系；
- API/事件/持久化上下文；
- 规则评分及其分项；
- 必要的短代码片段，包含文件和行号。

不得只给名称要求模型凭常识判断。

### 11.2 输出契约

LLM 必须返回结构化 JSON：

```json
{
  "relationship": "same|subset|wrapper|related|false|uncertain",
  "confidence": 0.0,
  "canonical_name": "Order",
  "reason": "字段和订单创建 API 上下文一致",
  "supporting_evidence_ids": ["ev-12", "ev-19"]
}
```

若 JSON 校验失败、引用不存在的证据、置信度不足或请求异常，应保留规则结果并标记 `llm_status`，不得丢弃已有结果。

### 11.3 安全边界

- LLM 不生成 SQL；
- LLM 不修改源码、词典或仓库配置；
- 用户私有代码发送策略由配置控制；
- prompt、模型、规则版本和响应摘要写入审计记录；
- 人工确认后才能将新业务缩写加入受控词典。

## 12. 阶段八：冲突处理与人工反馈

典型冲突及处理方式：

| 冲突 | 处理 |
|---|---|
| 同名但字段和上下文完全不同 | 建立两个带上下文限定的术语，不自动合并 |
| 不同名但结构和调用上下文一致 | 标记高置信别名候选 |
| DTO 只含实体部分字段 | 判定 `subset` 或 `wrapper` |
| 一个术语对齐多个服务模型 | 保留一对多关系并显示 bounded context |
| 规则与 LLM 结论冲突 | 标记 `needs_review`，不由 LLM 静默覆盖 |
| 人工判断与算法冲突 | 人工覆盖优先，并记录作用域与版本 |

人工反馈数据包括：接纳、拒绝、修改规范名、添加别名、修改关系、加入黑白名单。反馈应进入独立覆盖层，不能改写原始证据。

## 13. 数据模型

建议新增独立的术语存储，而不是塞入 EvolutionStore 的现有功能表。

### 13.1 `terms`

```text
id                 稳定 ID
repository_id      所属仓库或逻辑服务
canonical_name     规范名
normalized_name    规范化名称
term_type          entity/value_object/state/event/action/technical/unknown
domain_score       归一化领域分
status             accepted/candidate/rejected/needs_review
source             rule/llm/user_override
first_seen_commit  首次出现提交
last_seen_commit   最近出现提交
algorithm_version  算法版本
created_at
updated_at
```

### 13.2 `term_mentions`

```text
id, term_id, node_id, symbol_name, qualified_name,
file_path, start_line, end_line, commit_hash, mention_kind
```

### 13.3 `term_evidence`

```text
id, term_id, evidence_type, evidence_value,
weight, source_location, rule_id, created_at
```

### 13.4 `term_relations`

```text
id, source_term_id, target_term_id,
relationship, confidence, status,
score_breakdown, llm_status, algorithm_version
```

### 13.5 `term_overrides`

```text
id, scope, matcher, action, value,
reason, author, created_at, expires_at
```

稳定 ID 应基于“仓库身份 + bounded context + 规范概念键”生成，不能直接使用文件路径，以免重命名造成术语身份漂移。

## 14. 配置设计

目标仓库可在 `.codeevolution/terms.yml` 中覆盖默认规则：

```yaml
version: 1
include_paths:
  - src/domain/**
exclude_paths:
  - generated/**
  - vendor/**

abbreviations:
  acct: account
  sku: stock_keeping_unit

technical_suffixes:
  - DTO
  - VO
  - Controller
  - Repository

stop_terms:
  - BaseResponse
  - CommonResult

accepted_terms:
  - name: SKU
    type: value_object

thresholds:
  accept: 0.80
  review: 0.60
  cross_repo_candidate: 0.60
  llm_review: 0.70

llm:
  enabled: false
  send_source_snippets: false
```

配置解析失败时应指出文件和字段，不得退回默认值后静默继续。

## 15. 对外接口

### 15.1 CLI

建议新增明确命令，避免继续复用含义模糊的 `entities`：

```bash
# 单仓术语识别
codeevolution terms extract -r <repo> [--types entity,event,state] [--llm]

# 查看证据与评分
codeevolution terms explain -r <repo> --term Order

# 多仓对齐
codeevolution terms align [--service orders] [--service billing] [--llm]

# 导出
codeevolution terms export -r <repo> --format json|markdown
```

现有 `knowledge -s entities` 和 `entities --llm` 在迁移期保留，内部委托给新服务，并提示新命令。

### 15.2 API

```text
POST /api/terms/extract
GET  /api/terms?repository=...&type=...&status=...
GET  /api/terms/{id}
GET  /api/terms/{id}/evidence
POST /api/terms/align
POST /api/terms/{id}/review
```

列表接口返回摘要，证据和评分明细按需加载，防止大型仓库响应过大。

### 15.3 Web

Web 页面至少提供：

- 术语列表和类型/状态/服务筛选；
- 分数、规则命中和源码定位；
- 别名与跨服务关系图；
- 接纳、拒绝、改名和关系修正；
- 当前算法版本、索引时间与过期状态。

## 16. 增量识别与缓存

每次运行记录：CodeGraph `indexed_at`、Git commit、规则版本、配置哈希、模型配置摘要。

增量策略：

1. 找出新增、修改和删除的类型节点；
2. 扩展到其一跳关系邻居；
3. 只重算受影响候选的局部特征；
4. 图全局 PageRank 变化超过阈值时触发全量重算；
5. 只重新匹配受影响 blocking bucket；
6. 保留被删除术语的历史身份，状态改为 `retired`；
7. 算法或配置版本变化时明确使缓存失效。

首版可先全量计算确保正确性，再基于基准测试引入局部更新。

## 17. 可解释性与审计

每个接纳或拒绝结果至少能回答：

- 它来自哪个代码符号？
- 哪些字段、关系、路径或注解贡献了分数？
- 命中了哪些规则？
- 为什么与另一个术语合并或不合并？
- LLM 是否参与，参与了哪一步？
- 使用了哪个算法版本和配置哈希？

示例：

```json
{
  "canonical_name": "Order",
  "status": "accepted",
  "domain_score": 0.91,
  "score_breakdown": {
    "annotation": 0.20,
    "domain_path": 0.15,
    "field_structure": 0.18,
    "graph_centrality": 0.23,
    "api_usage": 0.15
  },
  "evidence": [
    "src/domain/Order.java:12 @Entity",
    "8 fields",
    "referenced by POST /orders"
  ]
}
```

## 18. 测试与评估

### 18.1 单元测试

- CamelCase、缩写、连续大写、数字和 Unicode 分词；
- 路径、注解、字段、关系的评分边界；
- 技术类型过滤；
- `same/subset/wrapper/related/false` 判定；
- 配置合并、非法配置和人工覆盖；
- LLM JSON 校验及降级。

### 18.2 集成测试

构造至少三个小型服务：订单、支付、用户，并包含：

- `Order`、`OrderDTO`、`PurchaseOrder`；
- `UserAccount`、`Account`、语义不同的 `LedgerAccount`；
- `PaymentEvent` 和 `PaymentMessage`；
- 同名不同义、不同名同义、一对多和删除重命名场景。

验证从 CodeGraph 数据到 CLI/API 输出的完整链路。

### 18.3 黄金标注集

人工为代表性仓库标注：

- 哪些候选是领域术语；
- 术语类型；
- 别名组；
- 跨仓关系；
- 应被排除的技术类型。

核心指标：

| 指标 | 目标 |
|---|---|
| 领域术语 Precision@默认列表 | ≥ 0.90 |
| 领域术语 Recall | ≥ 0.80 |
| `same` 对齐 Precision | ≥ 0.90 |
| 跨仓候选 Recall | ≥ 0.90 |
| 有完整证据的结果比例 | 100% |
| 无 LLM 运行成功率 | 100% |

按语言、框架、仓库规模和术语类型分别统计，不能只报告总体平均值。

### 18.4 回归测试

现有 `Product`/`ProductService` 排序、`UserService`/`UserSvc` 对齐行为应固定为迁移基线。算法升级导致结果变化时，必须输出对比报告，而不是直接更新快照掩盖回归。

## 19. 性能要求

初始目标：

- 10 万 CodeGraph 节点的单仓规则识别在普通开发机上不超过 10 秒；
- 候选匹配通过 blocking 避免全局 `O(n²)`；
- 默认不读取全部源码正文；
- LLM 请求批量化、可限额、可取消，且失败不阻塞规则结果；
- API 列表分页，证据按需读取；
- 基准结果记录候选数、比较对数量、内存峰值和各阶段耗时。

性能目标应在 `benchmark_large_repo.py` 的可复现实验上校准。

## 20. 分阶段实施计划

### M1：统一当前能力

- 新增 `TermCandidate`、`TermEvidence`、`TermRelation` 领域模型；
- 抽取统一名称规范化器；
- 将核心实体评分改成可解释的分项结果；
- 修正文档与实现不一致的缩写/编辑距离描述；
- 保持现有 CLI/API 输出兼容。

验收：现有测试全部通过，同一输入的核心实体排序不发生无解释变化。

### M2：单仓术语识别

- 引入准入阈值、技术类型分类、别名归并；
- 支持 `.codeevolution/terms.yml`；
- 增加术语存储、证据查询和 `terms explain`；
- 建立首批黄金标注集。

验收：默认列表 Precision 达到目标，所有结果具有源码证据和评分明细。

### M3：跨仓对齐升级

- 加入字段、API、消息和调用上下文；
- 使用 blocking + 精排；
- 支持一对多和最大权重一对一策略；
- 保存备选关系和冲突。

验收：`same` Precision 与候选 Recall 达到目标，不再依赖遍历顺序决定结果。

### M4：LLM 复核与人工反馈

- 实现证据包和严格 JSON schema；
- 增加隐私配置、审计与失败降级；
- Web 增加人工复核；
- 人工反馈写入覆盖层。

验收：关闭 LLM 时功能完整；启用后中置信候选准确率提高且不会破坏确定性结果。

### M5：增量与演进

- 按节点变化增量更新术语；
- 跟踪别名、规范名、拆分和合并事件；
- 与 Evolution Engine 的 commit 时间线关联；
- 增加大仓性能回归。

验收：小范围代码修改只重算受影响 bucket，删除和重命名不丢失术语历史。

## 21. 主要风险与应对

| 风险 | 应对 |
|---|---|
| 命名不规范导致召回不足 | 结合字段、API、消息和人工缩写表 |
| DTO/VO/Entity 被错误合并 | 使用 `subset/wrapper` 关系，只有 `same` 才归并 |
| 同名异义 | 引入 bounded context、字段和调用上下文 |
| LLM 幻觉 | 证据 ID 约束、JSON 校验、低置信转人工 |
| 多语言 CodeGraph 数据差异 | 按语言建立夹具和指标，不使用单一全局阈值 |
| 大仓两两比较过慢 | blocking、缓存和增量 bucket 重算 |
| 人工覆盖污染事实 | 覆盖层与原始证据分离，完整记录审计信息 |
| 算法升级造成结果漂移 | `algorithm_version`、配置哈希和差异报告 |

## 22. 完成定义

术语识别能力只有同时满足以下条件才视为完成：

- 能稳定识别至少实体、状态和事件三类领域术语；
- 单仓别名与跨仓关系有明确区分；
- 每个术语和关系均可追溯到代码位置及规则证据；
- 无 LLM 环境下可完整运行；
- LLM 失败时可安全降级；
- 支持配置、人工复核、算法版本和增量更新；
- 黄金标注集指标达到既定门槛；
- CLI、API、Web 对同一结果使用一致的数据模型与术语。

