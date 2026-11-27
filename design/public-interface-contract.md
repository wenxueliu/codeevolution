# 公开接口契约设计（SDK / Library 非 HTTP 公开面）

| 项目 | 内容 |
|------|------|
| 文档版本 | 1.0 |
| 状态 | 已实现（`interface_contract` v2） |
| 日期 | 2026-11-27 |
| 能力编号 | 能力矩阵 #19 |
| 核心代码 | `codeevolution/analysis/knowledge/interface_contract.py`、`codeevolution/application/snapshot_query_service.py`、`codeevolution/cli.py`、`codeevolution/api.py`、`web/src/pages/Knowledge.vue` |
| 数据来源 | 已发布 Snapshot 中冻结的 CodeGraph（`nodes` / `edges`） |
| 对外入口 | `knowledge --snapshot-id <id> -s interface`、`interface-contract`、`GET /api/knowledge?section=interface`、`GET /api/interface-contract/export` |

---

## 1. 背景

### 1.1 问题

SDK / Library 类仓库没有 HTTP 路由、MQ 通道或定时任务，`api_contract` 提取结果为空，
跨仓拓扑的可达性根也为空。这类仓库真正的契约是**公开接口面**（public surface）：
外部调用方被允许依赖的符号集合。

旧实现（`interface_contract` v1）只按以下条件筛选：

- 名字不以 `_` 开头；
- 路径不命中 `tests` / `internal` / `examples` / `docs` 等目录；
- TypeScript / JavaScript / Go 要求 `is_exported` 为真（Python 该字段不可信）。

它**没有使用调用图**，因此把「包内实现函数」和「前端客户端函数」也当成了公开接口。
同时 `KnowledgeExtractor._serialize_interface` 把结果截断到 200 条，但 `entry_count`
仍返回全量，界面出现「数量很多、列表只有 200 条」的错觉。

### 1.2 旧实现实测

| 仓库 | 旧实现 | 实际情况 |
|---|---:|---|
| `harness_framework`（Python SDK） | 489 条 | 包 `__init__.py` 只再导出 41 个符号；其余是仪表盘前端客户端、包内工具函数 |
| `codehistory`（服务） | 1046 条，只持久化前 200 条 | 其中 115 条是 HTTP 处理函数，与 API 契约重复 |

### 1.3 根因

1. **判定维度缺失**：没有「是否被本服务其他接口调用」这一维度。
2. **契约来源错误**：SDK 的契约是作者显式声明的导出面，不是「所有非下划线函数」。
3. **与 API 契约边界不清**：HTTP 处理函数在调用图里同样没有内部调用方，被误收进非 HTTP 面。
4. **静默截断**：`entry_count` 与 `entries` 不一致，且没有 `truncated` 标记。

---

## 2. 目标与非目标

### 2.1 目标

1. 公开接口 = 外部调用方真正可以依赖的那一层，而不是「所有非下划线函数」。
2. 为 SDK 业务逻辑提取提供正确入口：入口正确，调用树、业务描述、业务规则才有意义。
3. 作为跨服务拓扑的可达性根：SDK 的出站调用必须能从真实公开入口走到。
4. 作为对外兼容性基线：公开面减少或签名变化应被视为破坏性变更。
5. 与 API 契约互斥：同一符号不在两份契约中重复出现。

### 2.2 非目标

- 不替代 API 契约：HTTP 端点仍由 `api_contract` 负责。
- 不做跨仓/跨版本的 API diff（后续演进）。
- 不解析 `pyproject.toml` / `package.json` 的发布配置；只使用代码图与包入口约定。
- 不为每个入口调用 LLM 生成解释；业务描述/规则复用快照中已有的 LLM 知识。

---

## 3. 术语

| 术语 | 含义 |
|------|------|
| 公开面 / public surface | 外部调用方可以依赖的符号集合 |
| 显式导出 / declared | 包入口 `__init__.py` 通过相对导入再导出的名字 |
| 推断根 / inferred root | 没有显式导出时，调用图中没有非测试内部调用方的公共符号 |
| 内部调用方 | 本仓库内（非测试）调用该符号的函数/方法 |
| HTTP 处理函数 | 路由/装饰器/`http.server` 分发命中的函数或方法 |

---

## 4. 方案总览

### 4.1 架构位置

```text
        Snapshot（不可变）
   ┌─────────────────────────┐
   │ facts + 冻结 CodeGraph  │
   └────────────┬────────────┘
                │ 读边界重算（不改写 facts）
                ▼
   InterfaceContractExtractor（v2）
     ├─ 显式导出解析：__init__.py 相对导入
     └─ 推断根：候选 → 调用图/实例化/HTTP 过滤
                │
                ▼
   KnowledgeExtractor._serialize_interface
     { version, entry_count, declared_count, inferred_count,
       file_count, truncated, entries[] }
                │
     ┌──────────┼───────────────────────────┬────────────────────┐
     ▼          ▼                           ▼                    ▼
 GET /api/      Web 知识中心「公开接口」    CLI interface-      跨仓拓扑可达根
 knowledge      （表格/计数/导出）          contract（逐条文档） （仅无 HTTP 入口时）
```

设计要点：

- **只读冻结图谱**：公开面是冻结 CodeGraph 的纯函数，旧快照可在读取边界重算，不重新发布。
- **版本化**：`INTERFACE_VERSION = 2`；版本不匹配时用 `handle.graph` 重建。
- **全量返回**：公开面已经是过滤后的契约，1,000 条只是安全阀，且返回 `truncated`。
- **单一实现**：CLI 与 HTTP 导出共用 `cli.py` 的文档构造函数与 Markdown 渲染，避免漂移。

### 4.2 判定流程

```text
candidates = 所有 function/method 节点
      │
      ├─ R4 过滤：命名 / 可见性 / 路径 / 测试 / 语言导出标志
      ├─ R9 过滤：HTTP 处理函数（归 API 契约）
      │
      ├─ 包入口 __init__.py 有相对导入？
      │     ├─ 是 → 解析导入名 → 解析定义节点 → public_surface = declared
      │     └─ 否 ↓
      │
      ├─ 有非测试内部调用方？ ── 是 → 丢弃
      ├─ 是嵌套函数？ ────────── 是 → 丢弃
      ├─ 是方法且所属类被内部实例化？ ── 是 → 丢弃
      └─ 否则 → public_surface = root
```

### 4.3 与 API 契约的关系

| 维度 | API 契约 | 公开接口契约 |
|------|----------|--------------|
| 入口身份 | HTTP 方法 + 路径 | 模块 + 符号名 + 类型 |
| 契约内容 | headers / body / path / query / response schema | 签名、参数、返回、docstring、类公开方法 |
| 调用证据 | 处理函数调用链 | 直接下游调用（callee） |
| 对外性证据 | 路由/装饰器注册 | 显式导出，或无内部调用方 |
| 功能解释 | LLM 节点解释 | `feature_explanation`（来源 + 声明 + 调用统计） |
| 业务描述 | 聚合解释中的业务描述 | 快照中同名 `business_descriptions`（可选） |
| 业务规则 | 聚合解释中的 `business_rules` | 快照中同名 `business_rules`（可选） |
| 导出命令 | `codeevolution api-contract` | `codeevolution interface-contract` |

两者**互斥**：HTTP 路由处理函数、HTTP 装饰器处理函数、`http.server` 的
`do_<VERB>` 分发方法已经由 API 契约覆盖，不再出现在公开接口中。

---

## 5. 判定规则

### R1 显式导出优先

Python 包入口模块（`<pkg>/__init__.py` 或 `src/<pkg>/__init__.py`）中的相对导入：

```python
from .contracts import AgentContract, ArtifactManifest
from .evaluator import decide_evaluator_action
```

声明的名字就是公开面。别名（`from .core import create_client as build`）以**导出名**为准，
并在 `declared_name` 保留定义名。显式导出即使被包内代码调用，仍然公开——这是作者对外的
明确承诺。

只认相对导入：`from requests import Session` 是第三方符号，不是本仓库的公开面。

### R2 无内部调用方（推断模式）

没有显式导出声明时，公开面 = 调用图根集合。一个公共函数/方法只有在满足以下条件时入选：

- 仓库内**没有**任何非测试代码调用它（`edges.kind = 'calls'`）；
- 自递归调用不算调用方；
- 测试代码的调用**不**使其内部化（测试是外部消费者，不是服务内部接口）；
- 不是嵌套/局部函数（父节点为 function/method 的排除）。

这就是「公开接口必须是当前服务没有被其他接口调用的」规则：仓库内部还在调用的符号属于
实现细节，不属于契约。

### R3 类被内部实例化则其方法内部化

方法通过 `self.method()` / 多态分发调用时，CodeGraph 的 `calls` 边可能只指向基类方法，
子类实现看起来「无人调用」。因此补充：若方法所属类被仓库内非测试代码 `instantiates`，
则该类的方法不进入推断公开面——这个类是内部实现类型，实例是内部创建的。SDK 的公开类
通常由外部构造，或其工厂函数才是入口（工厂函数仍会入选）。

### R4 命名、可见性、路径与语言规则

- 名字为空或以 `_` 开头 → 排除；
- `visibility` 为 `private` / `protected` / `internal` / `module` → 排除；
- 路径段命中 `tests`、`test`、`internal`、`examples`、`docs`、`scripts`、`build`、
  `dist`、`node_modules` 等 → 排除；
- 测试路径/文件名（`tests/`、`test_*.py`、`*.spec.ts`、`conftest.py` 等）→ 排除；
- Vue 组件 → 排除（页面层由 `page_api_map` 负责）；
- TypeScript / JavaScript / Go → 要求 `is_exported` 为真（这三种语言该字段可信）；
- Python → 不依赖 `is_exported`（CodeGraph 0.9.x 对所有 Python 节点都给 0）。

### R5 结果完整返回，禁止静默截断

公开面本身就是过滤后的契约，按全量返回。序列化保留 `entry_count`、`declared_count`、
`inferred_count`、`file_count`、`truncated`；只有超过 1,000 条安全阀时才截断，且
`truncated = true`，不会再出现「计数 1046、列表 200」。

### R6 版本化重建

`INTERFACE_VERSION` 从 1 升到 2。读取旧快照时，`SnapshotQueryService._with_interface_contract`
检测到版本不匹配，用**快照冻结的 CodeGraph** 重新计算公开面，不修改不可变 facts，
也不需要重新发布快照。

### R7 稳定排序与去重

- 显式导出按 `(file_path, line, name)` 排序；同名去重键是 `(entry_path, exported)`，
  允许多个包导出同名符号；
- 推断根按 `functions()` 的 `(file_path, start_line)` 顺序输出；
- 别名导出保留 `declared_name`，避免与定义名混淆。

### R8 拓扑可达性根

跨仓分析中，若服务没有 HTTP 入口，公开面作为 `method = "SDK"` 的可达性根，SDK 的出站
调用由此进入跨服务拓扑。类入口会展开为该类的公开方法（类节点本身没有 `calls` 边），
避免出站调用断链。公开面收紧后，拓扑不再从内部工具函数错误出发。

### R9 与 API 契约互斥

以下节点属于 HTTP 面，统一从公开接口中剔除（即使它被包 `__init__` 重新导出）：

- 路由节点（`kind = 'route'`）命中的处理函数；
- 装饰器命中 `HTTP_DECORATORS`（`@app.get`、`@router.post`、`@GetMapping` 等）的函数/方法；
- `http.server` 的 `do_GET` / `do_POST` 等分发方法。

实现：`SQLiteCodeGraphRepository.http_handler_node_ids()` 汇总三类命中，提取器在
declared 与 inferred 两条路径上都按 `node_id` 排除。跨仓拓扑本来只在**没有 HTTP 入口**
时才把公开接口作为可达根，因此也不会与 HTTP 入口重复。

---

## 6. 能力设计

### 6.1 知识 section 数据形状

```json
{
  "version": 2,
  "entry_count": 41,
  "declared_count": 41,
  "inferred_count": 0,
  "file_count": 11,
  "truncated": false,
  "entries": [
    {
      "name": "ContextStore",
      "declared_name": null,
      "qualified_name": "ContextStore",
      "kind": "class",
      "signature": "ContextStore",
      "language": "python",
      "file_path": "harness_framework/context_store.py",
      "line": 19,
      "module": "harness_framework.context_store",
      "node_id": "class:...",
      "docstring": "",
      "public_surface": "declared",
      "declared_by": "harness_framework/__init__.py",
      "caller_count": 0,
      "internal_callers": [],
      "member_count": 7
    }
  ]
}
```

### 6.2 CLI

```bash
# 与知识中心同一份数据
codeevolution knowledge --snapshot-id <id> -s interface

# 每个公开入口一份 Markdown/JSON 契约
codeevolution interface-contract --snapshot-id <id>
codeevolution interface-contract --snapshot-id <id> --format json -o ./contracts
codeevolution interface-contract --snapshot-id <id> --server http://127.0.0.1:8765
```

### 6.3 HTTP API

```http
GET /api/knowledge?snapshot_id=<id>&section=interface&complete=true
GET /api/interface-contract/export?repository_snapshot_id=<id>&format=markdown
```

导出返回 `{repository_snapshot_id, format, entry_count, documents: [{filename, content}]}`，
与 `/api/api-explanations/export` 同构。

### 6.4 Web 控制台

知识中心「公开接口」页：

- 指标：总数 / 显式导出 / 推断根 / 文件数；
- 表格列：名称（含模块）、类型（类显示公开方法数）、签名、来源（declared/root + 声明文件）、
  内部调用方数量、文件:行；
- 操作：导出公开接口契约（下载 JSON，内容为逐条文档）；
- 中英文词条齐全，空状态说明「不含 HTTP 端点」。

### 6.5 导出文档结构

每个入口一份文档，字段与章节：

```text
# <Name> (<kind>)

## Public Surface
- Module / Source / Surface(declared|root) / Declared by / Internal call sites
- internal_callers[]

## Interface Contract
### Signature
### Docstring
### Public Methods      # 仅 class，最多 50 个
### Direct Calls        # 直接下游调用，最多 20 个

## Feature Explanation  # 来源 + 声明 + 调用统计
## Business Description # 快照同名 business_descriptions，缺失则 Not generated.
## Business Rules       # 快照同名 business_rules，缺失则 Not generated.
```

### 6.6 跨仓拓扑可达根

```python
if not inbound:  # 没有 HTTP 入口的 SDK
    for entry in InterfaceContractExtractor(reader).extract():
        roots = [entry]
        if entry.kind == "class":
            roots = 该类的公开方法
        for root in roots:
            inbound.append({"method": "SDK", "node_id": root.node_id, ...})
```

### 6.7 业务描述与规则

- 快照 facts 已有 `business_descriptions` / `business_rules` 时，按
  `qualified_name` → `name` 顺序匹配并写入导出文档；
- 没有 LLM 知识时显示 `Not generated.`，不伪造内容；
- 业务知识仍由既有 `-s business --llm` / `-s rules --llm` 流程生成，本能力只负责关联。

---

## 7. 数据与字段

| 字段 | 含义 |
|------|------|
| `name` | 对外名称（别名导出时为导出名） |
| `declared_name` | 别名导出时指向的定义名 |
| `qualified_name` | CodeGraph 限定名 |
| `kind` | `class` / `function` / `constant` / `variable` |
| `signature` | 源码签名 |
| `module` | 点分模块名（去掉 `src/` 与 `.py`） |
| `file_path` / `line` | 定义位置 |
| `node_id` | CodeGraph 节点 id，供调用链/导出使用 |
| `docstring` | 源码 docstring（显式导出节点可读） |
| `public_surface` | `declared`（显式导出）或 `root`（推断根） |
| `declared_by` | 声明它的包入口 `__init__.py` |
| `caller_count` | 仓库内非测试内部调用方数量（证据，不改变 declared 结论） |
| `internal_callers` | 前 5 个调用方（名字/文件/行） |
| `member_count` | 类公开方法数量 |
| `calls` | 导出文档中的直接下游调用（最多 20） |
| `members` | 导出文档中类的公开方法（最多 50） |

新增仓储查询：

| 方法 | 用途 |
|------|------|
| `package_entry_imports()` | 包入口 `__init__.py` 的相对导入语句 |
| `symbols_in_file()` | 文件内全部符号（含常量） |
| `node_kind()` | 判断嵌套函数的父节点类型 |
| `caller_edges()` | 全量 `calls` 边 + 调用方身份（一次构建索引） |
| `instantiation_edges()` | 全量 `instantiates` 边 |
| `http_handler_node_ids()` | HTTP 处理函数节点 id（R9） |

---

## 8. 兼容与迁移

| 场景 | 行为 |
|------|------|
| 旧快照（v1 facts） | 读取时检测版本，用冻结 CodeGraph 重算，不写回 facts |
| 旧快照无 `interface_contract` | 同样在读取边界投影；失败时返回原 facts |
| 无显式导出的仓库 | 自动走推断根，行为与旧版「名称过滤」兼容但更严格 |
| 前端旧字段 | 保留 `entries` / `entry_count` / `file_count`；新增字段向后兼容 |
| API 消费方 | 新增字段与接口，不改变既有 `api_contract` 结构 |
| CLI | 新增子命令，不影响既有命令 |

回滚策略：`INTERFACE_VERSION` 可回退到 1，旧快照将重新使用持久化 facts；但 1,000 条
安全阀与 HTTP 互斥会失效，因此不建议回滚。

---

## 9. 备选方案与取舍

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| 只解析 `__all__` | 作者声明最准确 | CodeGraph 的 `__all__` `signature` 被截断（实测 105 字符），无法可靠取全 | 否 |
| 只使用调用图根 | 规则简单、无需包布局 | 作者显式导出但内部复用的函数会被误删（如 `decide_evaluator_action`） | 否 |
| 只列模块级函数/类 | 数量少 | 漏掉类方法/常量，无法表达类契约 | 否 |
| 声明优先 + 调用图兜底（本方案） | 兼顾作者意图与内部实现识别 | 需要包入口约定，规则较多 | **采用** |
| 按名字与 API 端点去重 | 实现简单 | 跨文件同名误杀（MCP 工具 vs HTTP 处理函数） | 否，改为 node id/位置 |

---

## 10. 风险与边界

1. **动态分发/反射**：CodeGraph 的 Python 调用解析不覆盖所有分发；R3 的类实例化规则是
   主要补偿，仍可能残留少量内部方法。
2. **包布局**：只识别 `<pkg>/__init__.py` 与 `src/<pkg>/__init__.py`；monorepo 子包
   （`packages/foo/src/__init__.py`）当前不会作为包入口，需后续扩展。
3. **`__init__.py` 导入内部工具**：会被视为显式导出；这是作者声明，工具不二次猜测。
4. **HTTP 检测覆盖**：装饰器、路由表、`http.server` 分发可识别；通过
   `app.add_api_route(...)` 动态注册且无装饰器的处理函数可能漏检。
5. **常量 signature 截断**：CodeGraph 的变量 `signature` 可能被截断，导出只读图谱，不读源码。
6. **业务描述/规则为空**：快照未生成 LLM 知识时导出显示 `Not generated.`，属于预期行为。

---

## 11. 测试与验收

### 11.1 自动化

`tests/test_knowledge_components.py`：

- 显式导出优先于推断，且内部调用不改变 declared；
- 有内部调用方的函数被丢弃；
- 被内部实例化的类的方法被丢弃；
- 嵌套函数被丢弃；
- HTTP 处理函数在 inferred / declared 两条路径都被剔除；
- 250 条结果全量序列化，`truncated=false`。

`tests/test_delivery_adapters.py`：

- `GET /api/interface-contract/export` 每个入口一份文档，含 Public Surface 与业务描述。

`tests/test_public_contracts.py`：

- CLI 帮助包含 `interface-contract`；
- OpenAPI 路径快照包含新路由。

全量结果：`388 passed, 1 skipped`。

### 11.2 真实仓库回归

| 仓库 | 旧实现 | 新实现 | 说明 |
|---|---:|---:|---|
| `harness_framework` | 489 | 41 | 全部 declared，等于包 `__init__` 再导出 |
| `codehistory` | 1046（持久化 200） | 140 | 识别 139 个 HTTP 处理函数，其中 113 个原本混入公开接口；剔除后与 API 契约 0 重复 |

- 按「文件 + 行号」比对 API 端点：0 重复；
- 性能：单快照重建 0.08–0.13s；
- 前端构建通过，中英文词条齐全。

### 11.3 手工验收

```bash
codeevolution knowledge --snapshot-id <sdk-snapshot> -s interface
codeevolution interface-contract --snapshot-id <sdk-snapshot> -o /tmp/contracts
curl -s "http://127.0.0.1:8765/api/interface-contract/export?repository_snapshot_id=<id>"
```

---

## 12. 后续演进

1. **多包布局**：支持 `packages/*/src/<pkg>/__init__.py` 与 `pyproject.toml` 的
   `[project]` / `[tool.setuptools]` 导出声明。
2. **TS/JS 声明面**：解析 `package.json` `exports` 与 `index.ts` 的 `export` 语句，
   减少对 `is_exported` 的依赖。
3. **契约 diff**：对比两个快照的公开面，输出新增/删除/签名变化，作为兼容性门禁。
4. **按入口生成业务描述/规则**：把 LLM 解释键从 HTTP 端点扩展到公开接口节点。
5. **严格模式开关**：允许把 declared 但被内部调用的符号也剔除，用于「纯净对外面」审计。

---

## 附录 A：使用示例

```bash
# 1. SDK 公开面总览
codeevolution knowledge --snapshot-id ee790401-be19-44d3-9999-8e000963a634 -s interface

# 2. 逐条导出契约
codeevolution interface-contract \
  --snapshot-id ee790401-be19-44d3-9999-8e000963a634 \
  -o /tmp/harness-contracts

# 3. 通过 HTTP 导出
curl -s "http://127.0.0.1:8765/api/interface-contract/export?\
repository_snapshot_id=ee790401-be19-44d3-9999-8e000963a634&format=markdown"
```

## 附录 B：验收清单

- [x] 显式导出优先，支持别名/类/常量
- [x] 推断根剔除内部调用方、内部实例化类方法、嵌套函数
- [x] HTTP 处理函数与 API 契约互斥
- [x] 全量返回并标记 `truncated`
- [x] 旧快照读取边界重建（v2）
- [x] CLI / HTTP / Web / 拓扑四条消费路径
- [x] 中英文界面词条
- [x] 后端全量测试与真实仓库回归
