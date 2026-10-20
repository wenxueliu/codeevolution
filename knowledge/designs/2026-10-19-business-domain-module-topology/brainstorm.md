# 模块拓扑：按文件名划分业务模块（域 → 实体）

- 日期：2026-10-19
- 服务：`services/codehistory`（CodeEvolution）
- 影响面：`codeevolution/analysis/knowledge/module_topology.py` + `Knowledge.vue`
- 状态：待用户批准

## 1. 背景与目标

### 问题

当前模块拓扑用**目录路径**建骨架，因此输出的模块是**架构分层**：

```
mall-admin/controller    mall-admin/service    mall-mbg/model    mall-mbg/mapper
```

而用户要的模块拓扑对应的是**业务分类**（商品 / 订单 / 用户 / 营销 / 内容）。

根因：同一个业务实体的各个层被物理拆分到不同目录，路径丢失了业务归属。

```
com/macro/mall/controller/PmsProductController.java
com/macro/mall/service/PmsProductService.java
com/macro/mall/service/impl/PmsProductServiceImpl.java   ← 5 个目录
com/macro/mall/dao/PmsProductDao.java                      8 个层
com/macro/mall/mapper/PmsProductMapper.java
com/macro/mall/dto/PmsProductParam.java
com/macro/mall/model/PmsProduct.java
```

只有**文件名**（`PmsProduct*`）能把它们绑成一个业务实体。

### 目标

1. 模块拓扑改为**业务分类**：域 → 实体 的两层树。
2. 架构分层降级为**模块内属性**，不再决定模块归属。
3. 对文件名不含业务信号的仓库**自动回落**到现有路径骨架。
4. 把「文件名与所在结构不一致」作为**发现项**暴露，而不是用来决定归属。

### 依据（实测数据）

`mall` 519 个源文件：

| 轴 | 信号来源 | 证据 |
|---|---|---|
| 架构层 | 文件名**后缀** | `Mapper` 76、`Example` 76、`Service` 50、`Impl` 49、`Controller` 47、`Dao` 28 |
| 业务域 | 文件名**前缀** | `Ums` 111、`Pms` 100、`Sms` 78、`Oms` 63、`Cms` 44 |

- 5 个业务前缀覆盖 **396/519 = 76%** 源文件。
- 共 **20 个域**，其中 **14 个域含 ≥2 个实体**，合计覆盖 **451/524 = 86%**。
- 域 → 实体规模：`Ums` 30、`Pms` 27、`Oms` 19、`Sms` 16、`Cms` 12。
- 无业务前缀的 103 个文件（`Jwt*`、`WebLog`、`Alipay`、`CommonResult`、`Api`）是横切基础设施。

### 已证伪的假设（重要）

用户假设「文件名和文件中的类/函数不一致」是可用的设计信号。实测：

| 度量 | mall | codeos |
|---|---|---|
| 文件名 vs 文件内声明的类 | **0/516 不一致** | **0/50 不一致** |
| 目录层 vs 文件名后缀层 | 76/524 不一致 | — |

- 文件名与类名在两个 Java 仓库中 **100% 一致**，该信号为空。
- 真正的「不一致」是**目录层 ≠ 文件名后缀层**（mall 76 例），且几乎全部是 MyBatis 生成的
  `*Example.java` 放在 `model/` 下——机械原因，不是设计坏味。
- 结论：该信号降级为**只读发现项 `layer_conflict`**，不参与模块归属。

## 2. 范围

**做**
- 新增业务域/实体聚类，替代路径骨架作为默认模块树。
- 分层改为模块内属性 `layers`。
- 新增发现项 `layer_conflict`。
- 保留路径骨架作为回落分支。
- 前端展示域 → 实体嵌套。

**不做**
- 不改动图谱解析、导入/调用边抽取。
- 不引入 LLM 语义判断（聚类完全确定性、可复现）。
- 不翻译业务缩写（`Pms` 保持原样，含义由其子实体承载）。
- 不改动 `knowledge_service.py` 的多仓合并形状。

## 3. 备选方案与取舍

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| A. 文件名聚类替代路径 | 域 → 实体两层树，路径仅作回落 | 直接给出业务分类，解决根因 | 对无前缀仓库需回落逻辑 | **推荐** |
| B. 路径骨架 + `domain` 标签 | 保持目录树，附加属性 | 改动最小 | 没解决「模块=架构层」的根因，用户已明确否决 | 否 |
| C. 与图谱社区发现结合 | 用 Louvain 再聚 | 理论完备 | 不可复现、命名困难；本仓已删过该方案 | 否 |

## 4. 推荐方案

### 4.1 两个正交轴

- **业务轴（模块树）**：`domain` → `entity`，来自文件名 token。
- **架构轴（模块内属性）**：`layer`，来自文件名后缀，后缀未知时退回目录段。

### 4.2 算法

1. **源文件筛选**：复用现有过滤，额外排除 vendored/压缩/生成的前端资源
   （`*.min.js`、`index.js`、`*.d.ts`、`plugin*`）与测试目录。
2. **分词**：basename 按 CamelCase / 缩写 / 数字切分。
3. **实体键**：从尾部剥掉已知分层 token，剩下即业务实体。
   `PmsProductController` → `PmsProduct`；被剥掉的后缀给出 `layer`。
4. **域键**：前导缩写段 `^[A-Z][a-z]{1,3}(?=[A-Z])` → `Pms`；无则空。
5. **信号接受判据（按仓库）**：
   源文件数 `>= 20` 且 前 5 大命名域覆盖 `>= 50%` 时采用业务聚类，
   否则回落路径骨架。
   - `mall` 76% → 采用；`codeos` 26% → 回落；`mall-admin-web` 6% → 回落。
   - **回落不是失败**：已按业务组织的仓库，其目录本身就是业务域，路径骨架给出同样的答案。
     因此该判据可以保守取值而不损失正确性。
6. **建树**：`depth 0` 域（`Pms` / `shared`）→ `depth 1` 实体（`PmsProduct`）→ `depth 2` 层（可选）。
7. **横切桶**：无域键，或实体键属于横切词表（`util`/`config`/`common`/`base`/`api`/`exception`/`constant`/`result`）的文件归入 `shared`。
8. **耦合**：沿用现有有向边聚合，但在**实体层**计算——`PmsProduct → PmsCategory` 才是业务依赖。
   `cohesion` = 实体内部边占比；`afferent`/`efferent` = 指向/被指向的实体数。

### 4.3 报告形状

```
id, name, kind ("domain"|"entity"|"shared"|"layer"),
parent_id, depth, file_count, total_file_count, path,
layers {layer: count},
cohesion, afferent, efferent, layer_conflict
```

- `version` 升为 `module-topology/v3`。
- 已实现的读边界投影（`module-topology/v2` → v3）会自动让旧 snapshot 生效，无需重跑分析。
- `path` 对实体模块取公共目录前缀，可能退化（如 `com/macro/mall`），故 `layers` 才是有效信息。

### 4.4 数据流

```
冻结 CodeGraph ──> 源文件清单 ──> 文件名分词 ──> 实体键 / 域键
                                          │
                            信号接受判据 ──┴──> 接受：域→实体树
                                          └──> 拒绝：现有路径骨架
                                          │
     导入/调用有向边 ──> 实体级耦合 ──────┴──> ModuleTopology
```

### 4.5 失败处理

| 情况 | 处理 |
|---|---|
| 无源文件 / 无实体 | 返回空拓扑（现状） |
| 实体键为空 | 归入 `shared` |
| 后缀无法识别分层 | `layer` 退回目录段；仍无法判定则置空 |
| 信号判据不通过 | 回落路径骨架（保留现有代码路径） |
| 图谱边缺失 | 模块仍输出，耦合指标为 0 |

## 5. 测试策略

按项目约束：不做 UT，做 API 集成测试 + UI 自动化。

- **API 集成**：`mall` 断言得到业务树且含 `Pms → PmsProduct`；`codeos` 断言回落到路径骨架；
  断言 `version == module-topology/v3`、`layer_conflict` 存在。
- **回归**：现有 `tests/` 全绿；旧 snapshot 读边界投影仍返回 v3。
- **UI**：知识中心模块拓扑页渲染域 → 实体嵌套，中英文文案走 `i18n.js`。

## 6. 开放问题（留给规划阶段）

1. 80 个单文件实体是否折叠进域节点？（倾向：保留为叶实体，但 UI 默认折叠）
2. 分层 `layer` 是否作为 `depth 2` 展开，还是作为实体卡片上的分布条？（倾向：分布条，避免树过深）
3. `shared` 桶是否再按层细分？（倾向：细分，`shared/util`、`shared/config`）
4. 横切词表是否需要可配置？

## 7. 依赖与降级记录

- `sw-grill-docs`：**SKIPPED**。本服务无 `knowledge/CONTEXT.md` 与 ADR 存储，
  术语基线为 `docs/design.md`；已在验收前人工对照该文档，未发现冲突。
- `sw-strategic-planner`：本变更为单服务单文件范围，未启用。

## 8. 待批准的决策

- 模块粒度：**两层树（域 → 实体）** —— 用户已选。
- 其余均按第 4 节默认值执行，如无异议即进入实现。

---

## 9. 追加：LLM 业务分类（按需覆盖）

### 9.1 触发的问题

第 4.2 节的接受判据只认 CamelCase 域前缀（`PmsProductController`）。实测
`codehistory`（snake_case，208 个源文件）识别出 **0 个域**，恒走路径骨架；
即便把规则扩展到 snake_case，前 5 大域覆盖率也只有 20%，仍低于 50%。

结论：**业务划分是语义问题，正则只能做"文件名里有没有域前缀"的代理判断**。
因此保留正则作为快速通道，新增按需的 LLM 分类覆盖层。

### 9.2 分层

```
启发式 (regex)  ──命中──> 业务树，source = heuristic
      │未命中
      └──> 目录骨架，source = paths + 提示"可请求 LLM 分类"

用户点击「按业务分类（LLM）」
      └──> source = llm（存库，读边界覆盖）
```

- 默认路径不变：分析阶段**不调用** LLM。
- LLM 结果存 `business_taxonomies`，按 `snapshot_id` 唯一；删除即回到启发式/路径骨架。
- `MODULE_TOPOLOGY_VERSION` 升至 `module-topology/v4`（新增 `source`、`label`、`label_en`、`description`）。

### 9.3 为什么是两段式

直接把 208 条路径 + 符号交给 7B 模型，实测只归类 12 个文件，且 8192 token 输出耗尽后 JSON 截断。
改为：

1. **确定性抽实体**：文件名剥掉分层词（`service`/`sqlite`/`resolver`…）得到实体键，
   `PmsProductServiceImpl` 与 `snapshot_query_service` 用同一套规则。
   这一步不需要模型，且保证每个文件都有归属候选。
2. **模型提域**：只给目录摘要，输出 6–12 个业务域（不含文件清单）。
3. **模型归属**：按 40 个实体一批，输出 `实体 -> 域 id`；漏掉的实体重问最多 3 轮。
4. **确定性展开**：实体 → 文件由代码完成。

实测 `codehistory`：9 个域 5.5s，4 批实体合计约 40s，最终 208/208 文件归类。

### 9.4 校验与失败可见

- 模型只能划分给定实体：未知/重复实体丢弃；未知域 id 丢弃。
- 未提及的实体 → `shared/unclassified`，并在 `issues` 与 UI 提示条里显示。
- 一次重试（域调用）后仍无域 → 任务 `failed`，错误写进 `job.error_message`。
- 缓存键：`snapshot_id + prompt_version + model + inventory_digest`；命中则 `cached: true`，不重复计费。

### 9.5 接口

```
POST   /api/business-taxonomy   {snapshot_id, hint?}  -> 202 {job}
GET    /api/business-taxonomy?snapshot_id=...         -> {taxonomy, job}
DELETE /api/business-taxonomy?snapshot_id=...         -> {deleted}
```

`hint` 是可选的一句话业务背景，与 `pyproject.toml` 的 description 一起进提示词。
任务复用现有 LLM 知识抽取的持久化模式（`business_taxonomy_jobs`，进程重启可恢复）。

### 9.6 已验证

- `codehistory`：11 个域、124 个模块、208/208 文件归类，`source = llm`。
- 删除分类后立即回到 13 个目录模块，`source = paths`。
- `harness_framework`：仍为路径骨架，UI 显示「按业务分类（LLM）」入口。
- 中英文界面均验证；后端 `297 passed, 1 skipped`，前端 `60 passed`。

---

## 10. 追加：树形下钻与证据链

### 10.1 问题

第 9 节的 LLM 分类落地后，页面把 124 个模块一次性平铺成卡片、只靠 `marginLeft`
缩进表示层级。结果：看不到"业务域"这个层次，无法判断某个实体该不该在它下面，
也没有任何依据可供人工校验——模块名只是模型的一句断言。

### 10.2 结构

```
业务域（默认展开层）
└── 实体
    └── 文件（完整路径）
        └── 类 / 函数（CodeGraph 声明，method 折叠为计数）
```

- 默认只渲染顶层（LLM 模式下即业务域；路径模式下即顶层目录），点击逐层展开。
- 文件列表随拓扑一起返回（`modules[].files`，取自既有 `owner` 映射，不新增计算）。
- 类/函数不放进拓扑 payload（一个仓库上千个），改为按模块懒加载。

### 10.3 证据接口

```
GET /api/module-evidence?snapshot_id=...&module_id=...
-> {module_id, file_count, files:[{path, symbols:[{name,kind}], method_count}]}
```

- 符号来自 `CodeGraph.file_symbols()`，仍按冻结图查询，不重新解析源码。
- `method` 不逐条渲染，只给 `method_count`：1100 个方法会淹没证据本身。
- 模块不存在 → 404，前端提示条显示错误，不静默空白。

### 10.4 交互

- 折叠状态在 `expandedModules`，文件/符号缓存在 `moduleEvidence`，同一模块只请求一次。
- 刷新结构知识或切换快照时清空，回到"只看业务域"。
- 中英文文案走 `i18n.js`；模块标题按 `label` / `label_en` 取当前语言。

### 10.5 已验证

- `codehistory`：12 行顶层（11 域 + shared）→ 展开域得实体 → 展开实体得文件 →
  文件下直接列出类/函数（如 `codegraph_capture.py` 的 `CodeGraphCapture` 与 7 个函数）。
- `harness_framework`：路径模式同样按顶层目录 → 子目录 → 文件 → 符号展开。
- `/api/module-evidence` 实测 70–180ms；后端 297 passed，前端 60 passed。

---

## 11. 追加：只看实现，只看公开面

### 11.1 排除测试代码

`codehistory` 208 个源文件里有 59 个是测试（`tests/` 80 个文件里 79 个是代码）。
测试镜像生产模块，不构成业务能力，却让树几乎翻倍，还把"这个实体到底由哪个文件实现"
稀释成"可能是一堆 test_*"。

`ModuleTopologyExtractor.is_test_path` 在 `_source_records` 阶段过滤，判据：

- 路径段包含 `test`/`tests`/`__tests__`/`testing`/`testdata`/`spec`/`specs`/
  `cypress`/`playwright`/`__mocks__`/`mocks`/`fixtures`；
- 文件名匹配 `test_*.py`、`conftest.py`、`*_test.{py,go,rs,rb,exs,js,ts}`、
  `*.{spec,test}.{js,ts,tsx,jsx,vue,mjs,cjs}`、`*{Test,Tests,IT,Spec,TestCase}.{java,kt,scala,cs,php}`。
- 文件名匹配**区分大小写**：`FooIT.java` 是测试，`Await.java` 不是。

效果：`codehistory` 208 → 149 个文件，124 → 97 个模块，实体 111 → 84。

### 11.2 只展示公开符号

`/api/module-evidence` 只返回不以 `_` 开头的类/函数/组件；方法同样只计公开方法。
证据链的用途是"让人一眼认出这个模块做什么"，`_file_sha256`、`_canonical` 这类
内部工具只会淹没结论。`codegraph` 实体因此从 11 个符号收敛到 4 个真正对外的东西。

### 11.3 统计口径同步

分类结果落库时的 `stats` 记录的是当时的文件清单，而拓扑过滤（测试、生成物）会变化。
读边界用实际服务的模块重新计算 `file_count / assigned_count / unclassified_count /
domain_count / entity_count`，避免页面出现"208/208 归类"但树里只有 149 个文件的矛盾。

### 11.4 已验证

- `codehistory`：11 个业务域、84 个实体、149/149 文件、0 个测试文件。
- 展开 `codegraph` 实体：4 个实现文件，符号只剩 `CodeGraphCapture`、`freeze_sources` 等公开面。
- `harness_framework`：`tests/`（80 个文件）整体消失，顶层只剩 4 个真实模块。
- 297 passed, 1 skipped；ruff clean。
