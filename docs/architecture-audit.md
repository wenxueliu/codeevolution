# CodeHistory 功能完整性与架构优化审计

> 审计日期：2026-08-01  
> 范围：重构规划、当前生产实现、交付入口和测试门禁

## 实施状态（2026-08-01）

本审计提出的架构迁移已按 P0 → P1 → P2 顺序实施。当前状态如下：

- P0 已完成：知识算法已拆分，旧模块为兼容 facade；CLI、API、MCP 已统一经过 application service；Evolution 查询已下推 Store repository；CodeGraph SQL 已全部收敛到 typed SQLite adapter；核心与全量覆盖率门禁及 facade 深比较测试已建立。
- P1 已完成：三服务 E2E fixture 覆盖 HTTP、MQ、gRPC、DB、循环、菱形及未知目标；Web 已使用统一 API client、`useAsync` 和错误模型；`create_app(dependencies)` 支持 service/store 注入；CI 已包含 lint、双覆盖率、Python build、Web test/build 和性能门禁。
- P2 已完成的静态能力：HTTP/MQ/gRPC/DB flow 均保存匹配规则、证据、置信度和规则版本；框架检测规则可配置；L2/L3 feature matcher 已实现；Topology 支持按 CodeGraph DB 指纹增量复用；大型仓库 benchmark 已建立。
- P2 已建立但仍需部署侧接入：运行时拓扑校验器可聚合标准化 OpenTelemetry span，输出 confirmed、static-only、runtime-only、调用次数和平均延迟；实际 OTLP collector/exporter adapter 仍需根据部署环境接入。
- 已实现：L2/L3 匹配触发的 `RENAMED`、`SPLIT`、`MERGED` 显式演进事件。
- 尚未实现：外部 trace/log/error 的 collector adapter 与持久化关联；它属于部署相关产品增强，不再阻塞本次架构迁移闭环。

当前自动门禁实测：核心覆盖率 60.95%，全量覆盖率 46.76%；大型查询 benchmark 在 2,000 features 下执行 2 条 SELECT，峰值内存约 0.10 MiB。

## 1. 总体结论

CodeHistory 的产品能力框架基本完整，已经覆盖单仓知识提取、多仓拓扑分析和功能演进追踪三条主线。

但从实现状态看，当前更准确的定位是：**新的分层架构和兼容边界已经建立，部分基础设施已完成迁移，真实主流程仍有较多逻辑运行在旧的大型模块中。** 因此，重构的阶段性结构已经落地，但“旧模块仅保留委托逻辑”的最终目标尚未完全达到。

下一阶段应优先让新架构接管真实执行路径，而不是继续扩充能力清单。

## 2. 已具备的整体能力

### 2.1 单仓知识提取

当前支持从 CodeGraph 数据中整理：

- API 契约
- 模块拓扑
- 核心实体
- 测试缺口
- 分层违规
- 配置消费
- 外部依赖
- 权限模型
- 调用热力图
- 基于 LLM 的业务描述、业务规则、错误场景和状态机

### 2.2 多仓微服务分析

当前支持：

- HTTP 跨服务调用匹配
- MQ topic 生产和消费匹配
- gRPC/RPC 静态调用分析
- 服务依赖拓扑
- 跨服务影响分析
- 端到端流程追踪
- 跨服务实体对齐
- Registry、服务发现、健康检查和 topology cache

### 2.3 功能演进追踪

当前支持：

- 沿 Git 历史分析 commit
- 识别 HTTP、CLI、事件和定时任务入口
- 以入口点与调用树为功能单位
- 记录功能出现、增长、收缩、消失和恢复
- 保存 snapshot、事件和调用链
- 通过 CLI、API 和 MCP 查询演进历史

## 3. 主要实现遗漏

### 3.1 知识提取器尚未真正拆分

`analysis/knowledge/` 已建立各维度模块，但多数类仍只是继承统一的 `ExtractionStep`，类体没有独立算法。真实知识提取逻辑仍集中在超过 1600 行的 `knowledge.py` 中。

影响：

- `knowledge.py` 仍承担大部分核心职责。
- 各知识维度无法独立演进和测试。
- `KnowledgeExtractor` 尚不是纯兼容 facade。

建议：按照 API、module、entity、test、layer、config、dependency、authorization、heatmap、semantic 的顺序逐项迁移真实算法。

### 3.2 应用服务尚未统一三个交付入口

已经建立 Evolution、Knowledge、Topology 和 Repository application service，但当前主要由测试使用。生产入口中仅 MCP 的部分 feature 列表逻辑使用 `EvolutionQueryService`。

CLI 仍直接构造或调用：

- `CodeGraphReader`
- `KnowledgeExtractor`
- `CrossRepoAnalyzer`
- Registry cache 函数

API 也仍以 `EvolutionStore` 为主要业务入口。

建议：按 CLI → API → MCP 的顺序迁移，使 delivery 只做参数校验、DTO 映射和渲染。

### 3.3 API 仍有直接 SQL 和 N+1 查询

feature 列表会对每个 feature 分别查询 timeline 和 snapshot；事件列表与事件统计仍直接执行 `store.conn.execute()`。

影响：

- feature 数量增加后查询次数线性增长。
- API 层依赖 Evolution SQLite schema。
- repository/application 边界没有闭合。

建议在 Store repository 增加：

- SQL 过滤和分页
- 总数查询
- 批量 event count
- 批量 latest snapshot
- event 聚合统计

### 3.4 CodeGraph SQLite 尚未收敛到唯一 adapter

虽然已有 `infrastructure/codegraph_sqlite.py`，但 `codegraph_reader.py` 和 `registry.py` 仍直接建立 SQLite 连接并执行查询。

建议：

1. 将所有 CodeGraph SQL 迁入 `SQLiteCodeGraphRepository`。
2. 为 schema 差异、NULL、坏 JSON 和缺列建立 repository contract test。
3. 将 `CodeGraphReader` 改为只委托 repository 的兼容 facade。

### 3.5 拓扑与流程模块主要仍是委托包装

`TopologyBuilder`、`ImpactAnalyzer` 和 `FlowTracer` 已存在，但主要调用旧 `CrossRepoAnalyzer` 方法。真实逻辑仍集中在 `cross_repo.py` 和 `p2_advanced.py`。

规划中的以下组件尚未形成独立实现：

- `ServiceInspector`
- HTTP/MQ/RPC collector
- `EntityAligner`
- `ServiceDetector`
- `RepositoryDiscovery`
- `HealthChecker`

建议优先提取纯 collector 和 matcher，再迁移 topology、impact、flow 编排。

### 3.6 DB flow 尚未实现

`FlowStep.channel` 和 renderer 声明了 `db` 类型，但当前没有数据库访问 collector 或 DB flow 边构建逻辑。真实流程分析主要覆盖 HTTP、MQ 和 gRPC。

可选择：

- 实现 SQL/table collector 和 service-to-database flow；或
- 在实现完成前，从公开能力描述中移除 DB flow 声明。

### 3.7 Web 交付层尚未完成规划迁移

Vue 页面仍分别直接调用 `fetch()`，部分异常使用空 `catch` 吞掉。统一 API client、`useAsync`、错误模型和加载状态尚未实现。

建议提取：

- `apiClient`
- repo query 编码函数
- 统一 HTTP error
- `useAsync` loading/error/data 状态
- 可测试的 fetch adapter

### 3.8 覆盖率口径不能代表全量生产代码

当前 63.37% 是核心单元覆盖率，统计时排除了 API、CLI、MCP、Knowledge、CrossRepo、P2、Registry、LLM 和 CodeGraphReader 等兼容或交付模块。

这些模块仍包含大量真实生产逻辑，因此该数字不等同于项目全量覆盖率。全量代码按同一统计思路计算时明显更低。

建议同时维护两个指标：

- 核心单元覆盖率：用于 domain/analysis/application/infrastructure。
- 全量覆盖率：覆盖所有 production modules，作为最终迁移完成门禁。

## 4. 产品能力可继续补充的部分

### 4.1 Evolution 身份匹配

当前主要依赖入口签名精确匹配。仍可实现：

- L2：调用树结构相似度
- L3：内容指纹或语义匹配
- rename/split/merge 的显式演进事件

### 4.2 运行时数据校验

接入 OpenTelemetry trace 后，可以：

- 校验静态推断的跨服务边
- 标记只在运行时出现的调用
- 为拓扑边增加调用频率和延迟
- 将 trace/log/error 关联到 feature timeline

### 4.3 分析结果的可解释性

建议为每条推断结果保存：

- 匹配规则
- 原始证据位置
- 置信度
- 未匹配原因
- 使用的框架或语言规则版本

### 4.4 大型仓库性能

建议增加：

- 增量 topology 构建
- repository 层批量查询
- SQLite 索引检查
- 结果分片和流式序列化
- 大仓 benchmark 与内存上限

### 4.5 真实端到端验证

需要两到三个微服务 fixture，至少覆盖：

- HTTP
- MQ
- gRPC
- 循环拓扑
- 菱形拓扑
- 未知目标
- 缓存命中、失效和强制刷新
- 旧 facade 与新 service 输出深比较

## 5. 优化优先级

### P0：完成架构迁移闭环

1. 将知识算法迁入 `analysis/knowledge/`。
2. 将 CodeGraph raw SQL 全部迁入 typed repository。
3. 将 Evolution 查询过滤、分页、统计和批量加载下推到 repository。
4. 让 CLI、API、MCP 统一调用 application service。
5. 将旧模块压缩成真正的兼容 facade。
6. 使用全量覆盖率证明迁移前后行为一致。

### P1：补齐真实性与可靠性

1. 增加真实微服务 E2E fixture。
2. 完成 Web API client 和统一错误处理。
3. 实现 DB flow，或修正文档声明。
4. 让 `create_app(dependencies)` 真正注入 store、repository 和 service。
5. 在标准 CI 环境运行 `pytest-cov`、`python -m build` 和 Web build。

### P2：提升分析质量

1. 为拓扑边增加证据和置信度。
2. 将框架规则插件化、配置化。
3. 接入运行时 trace 校验静态拓扑。
4. 实现 L2/L3 feature matcher。
5. 增加增量计算和大型仓库性能基准。

## 6. 推荐验收标准

只有同时满足以下条件，才建议将重构状态定义为“完全完成”：

- `knowledge.py`、`cross_repo.py`、`p2_advanced.py` 等旧模块仅保留委托逻辑。
- delivery 不直接执行 SQL 或分析算法。
- CLI、API、MCP 全部复用 application service。
- CodeGraph SQLite 仅由 repository adapter 访问。
- Web 使用统一 API client 和错误模型。
- 核心与全量覆盖率均达到约定门禁。
- 微服务 E2E fixture 覆盖 HTTP、MQ、gRPC 和复杂拓扑。
- Python package build 与 Web production build 均在 CI 通过。
