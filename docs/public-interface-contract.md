# 公开接口契约（SDK / Library）

> 对应能力：知识中心「公开接口」、`GET /api/knowledge?section=interface`、
> `codeevolution interface-contract`、`GET /api/interface-contract/export`。
> 版本：`interface_contract.version = 2`。

## 1. 功能解释

SDK / Library 类仓库没有 HTTP 路由、MQ 通道或定时任务，`api_contract` 提取结果为空，
拓扑可达性根节点也为空。这类仓库真正的契约是**公开接口面**（public surface）：
外部调用方被允许使用的符号。

公开接口契约对每个公开入口产出一份与 API 契约同构的文档：

| 内容 | API 契约 | 公开接口契约 |
|------|----------|--------------|
| 入口身份 | HTTP 方法 + 路径 | 模块 + 符号名 + 类型 |
| 契约 | headers / body / path / query / response schema | 签名、参数、返回、docstring |
| 调用证据 | 处理函数调用链 | 直接下游调用（callee） |
| 对外性证据 | 路由/装饰器注册 | 显式导出，或无内部调用方 |
| 功能解释 | LLM 节点解释 | `feature_explanation`（来源 + 声明 + 调用统计） |
| 业务描述 | `main_node_explanation.aggregate_explanation` | 快照中同名 `business_descriptions`（可选） |
| 业务规则 | 聚合解释中的 `business_rules` | 快照中同名 `business_rules`（可选） |
| 导出 | `codeevolution api-contract` | `codeevolution interface-contract` |

两者**互斥**：HTTP 路由处理函数、HTTP 装饰器处理函数、`http.server` 的
`do_<VERB>` 分发方法已经由 API 契约覆盖，不再出现在公开接口中；公开接口只描述
非 HTTP 的对外可访问面（函数、类、常量）。因此同一符号不会在两份契约里重复出现。

## 2. 业务描述

### 问题

旧实现只按「名字不以 `_` 开头、路径不在 tests/internal 下、TS/JS/Go 的
`is_exported` 为真」筛选，把**包内实现函数**也当成了公开接口：

- `harness_framework`：489 条，其中绝大多数是 `agent_dashboard` 前端客户端和包内工具函数，
  真正的 SDK 导出只有 41 条。
- `codehistory`：1046 条候选，但持久化时只保存前 200 条，`entry_count` 仍显示 1046，
  界面出现「数量很多、实际列表只有 200 条」的错觉。

### 目标

1. 公开接口 = 外部调用方真正可以依赖的那一层，而不是「所有非下划线函数」。
2. 为 SDK 业务逻辑提取提供正确入口：入口正确，调用树、业务描述、业务规则才有意义。
3. 作为跨服务拓扑的可达性根：SDK 的出站调用必须能从真实公开入口走到。
4. 作为对外兼容性基线：公开面减少/签名变化应被视为破坏性变更。

### 使用场景

- **产品 / 架构**：确认 SDK 对外承诺了哪些能力、分布在哪些模块。
- **开发**：新增符号时判断它是否会进入公开面；找出「本不该公开」的导出。
- **测试**：以公开面为契约测试清单，内部函数不进入稳定契约。
- **跨仓分析**：SDK 作为服务成员时，用公开面作为流程追踪的起点。

## 3. 业务规则

### R1 显式导出优先

Python 包入口模块（`<pkg>/__init__.py` 或 `src/<pkg>/__init__.py`）中的相对导入

```python
from .contracts import AgentContract, ArtifactManifest
from .evaluator import decide_evaluator_action
```

声明的名字就是公开面。别名（`from .core import create_client as build`）以**导出名**为准，
并在 `declared_name` 保留定义名。显式导出即使被包内代码调用，仍然公开——这是作者对外的
明确承诺。

> 只认相对导入：`from requests import Session` 是第三方符号，不是本仓库的公开面。

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
子类实现看起来「无人调用」。因此补充一条：若方法所属类被仓库内非测试代码
`instantiates`，则该类的方法不进入推断公开面——这个类是内部实现类型，实例是内部创建
的。SDK 的公开类通常由外部构造，或其工厂函数才是入口（工厂函数仍会入选）。

### R4 命名、可见性、路径与语言规则

沿用并收紧原有规则：

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
`inferred_count`、`file_count`、`truncated`；只有超过 1000 条安全阀时才截断，且
`truncated = true`，不会再出现「计数 1046、列表 200」。

### R6 版本化重建

`INTERFACE_VERSION` 从 1 升到 2。读取旧快照时，`SnapshotQueryService._with_interface_contract`
检测到版本不匹配，用**快照冻结的 CodeGraph** 重新计算公开面，不修改不可变 facts，
也不需要重新发布快照。

### R7 稳定排序与去重

- 显式导出按 `(file_path, line, name)` 排序，同名符号只保留一次；
- 推断根按 `functions()` 的 `(file_path, start_line)` 顺序输出；
- 别名导出保留 `declared_name`，避免与定义名混淆。

### R8 拓扑可达性根

跨仓分析中，若服务没有 HTTP 入口，`InterfaceContractExtractor` 的公开面作为
`method = "SDK"` 的可达性根，SDK 的出站调用由此进入跨服务拓扑。公开面收紧后，
拓扑不再从内部工具函数错误出发。

### R9 与 API 契约互斥

以下节点属于 HTTP 面，统一从公开接口中剔除（即使它被包 `__init__` 重新导出）：

- 路由节点（`kind = 'route'`）命中的处理函数；
- 装饰器命中 `HTTP_DECORATORS`（`@app.get`、`@router.post`、`@GetMapping` 等）的函数/方法；
- `http.server` 的 `do_GET` / `do_POST` 等分发方法。

实现：`SQLiteCodeGraphRepository.http_handler_node_ids()` 汇总三类命中，提取器在
declared 与 inferred 两条路径上都按 `node_id` 排除。跨仓拓扑本来只在**没有 HTTP 入口**
时才把公开接口作为可达根，因此也不会与 HTTP 入口重复。

### 已知边界

- CodeGraph 的 Python 调用解析不覆盖所有动态分发/反射；R3 的类实例化规则是主要补偿。
- 一个包 `__init__.py` 若把内部工具也导入进来，它们会被视为显式导出；这是作者声明，
  工具不二次猜测。
- 只有单层包或 `src/<pkg>` 布局会被识别为包入口；多级 monorepo 子包请以子包自己的
  `__init__.py` 为准（当前不递归识别）。
- `business_description` / `business_rules` 只有在快照已包含对应 LLM 知识时才填充，
  否则导出文档显示 `Not generated.`。

## 4. 判定流程

```text
candidates = 所有 function/method 节点
      │
      ├─ R4 过滤：命名 / 可见性 / 路径 / 测试 / 语言导出标志
      ├─ 是 HTTP 处理函数？ ── 是 → 丢弃（归 API 契约）
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

## 5. 字段说明

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
| `calls` | 导出文档中的直接下游调用 |

## 6. CLI / API

```bash
# 查看知识中心同一份数据
codeevolution knowledge --snapshot-id <id> -s interface

# 导出每个公开入口一份 Markdown/JSON 契约（含签名、直接调用、可选业务描述与规则）
codeevolution interface-contract --snapshot-id <id>
codeevolution interface-contract --snapshot-id <id> --format json -o ./contracts
codeevolution interface-contract --snapshot-id <id> --server http://127.0.0.1:8765
```

```http
GET /api/knowledge?snapshot_id=<id>&section=interface&complete=true
GET /api/interface-contract/export?repository_snapshot_id=<id>&format=markdown
```

`/api/interface-contract/export` 返回 `{documents: [{filename, content}]}`，与
`/api/api-explanations/export` 同构，Web 控制台「公开接口」页的「导出公开接口契约」
按钮直接下载该 JSON。

## 7. 验收

- 后端：`tests/test_knowledge_components.py`
  - 显式导出优先于推断，且内部调用不改变 declared；
  - 有内部调用方的函数被丢弃；
  - 被内部实例化的类的方法被丢弃；
  - 嵌套函数被丢弃；
  - 250 条结果全量序列化，`truncated=false`。
- 真实仓库回归（手工）：
  - `harness_framework`：489 → 41（全部 declared，等于包 `__init__` 再导出）；
  - `codehistory`：1046 → 140（推断根；无显式导出；已剔除 139 个 HTTP 处理函数）。
- 前端：公开接口页显示「总数 / 显式导出 / 推断根 / 文件数」，表格含「来源」「内部调用方」，
  中英文词条齐全；导出按钮走 `/api/interface-contract/export`。
