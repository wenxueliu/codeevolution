# 业务规则分层聚合设计（接口 → 分组 → 服务 → 跨服务）

| 项目 | 内容 |
|------|------|
| 文档版本 | 1.0（草案） |
| 状态 | Phase 1 已实现（API/CLI/UI），Phase 2–4 设计中 |
| 日期 | 2026-11-28 |
| 目标 | 从接口级业务规则自底向上聚合出分组规则、服务规则、跨服务规则，并保持完整证据链 |
| 依赖能力 | #1 API 契约、#11 业务规则、#12 错误目录、#14 统一服务拓扑、#19 公开接口契约 |
| 现有资产 | `api_contract`、API 解释快照（`aggregate_explanation.business_rules`）、`NodeRuleService`、`snapshot_rule_*`、`module_topology`、`TopologyArtifact` |

---

## 1. 背景与目标

### 1.1 现状

系统已经能产出**接口级**业务事实：

- API 解释快照的 `aggregate_explanation.business_rules`：每个端点调用链聚合出的规则，
  带 `source_node` 与 `evidence_lines`；
- `NodeRuleService` / `snapshot_rule_*`：调用链单个节点的业务规则（`rule_kind='node'`）；
- `business_rules` 知识 section：按函数抽取的 `rule_type / condition / failure_mode`；
- `module_topology` 与 `business_taxonomy`：模块/业务域划分；
- `TopologyArtifact`：跨服务调用边、跨服务流程。

但这些规则是**平铺的、按节点/端点散落**的，没有形成可复用的层次结构，也没有跨接口去重、
冲突识别和服务级/跨服务级合成。

### 1.2 目标

构建一条可追溯的规则聚合链：

```text
接口级规则（API 端点 / 公开接口）
        │  归并、去重、分类
        ▼
分组级规则（API 资源组 / 接口模块组）
        │  继承、提炼、冲突检测
        ▼
服务级规则（一个仓库/成员）
        │  跨服务对齐、契约组合
        ▼
跨服务规则（调用链上的义务、约束、不变量）
```

每一层都必须满足：

1. **可追溯**：每条聚合规则能回到原子规则、接口、源码行；
2. **可复现**：同样的输入与提示词版本得到同样的规则集合（digest 稳定）；
3. **可解释**：区分「继承的共享规则」与「本层新增/特化规则」；
4. **不臆造**：聚合只允许合并、归纳、分类已有事实，不允许新增未经证据支持的规则；
5. **冲突显式**：矛盾规则必须标记并保留双方证据，不能静默覆盖。

### 1.3 非目标

- 不替代现有 API 解释/节点解释，只在其上构建规则层；
- 不做运行时策略引擎（本设计产出的是业务规则知识，不是可执行策略）；
- 不在第一阶段引入向量库/图数据库，先用 SQLite + 确定性算法。

---

## 2. 分组模型

### 2.1 先分契约面，再分组

| 契约面 | 分组键 | 二级 | 示例 |
|--------|--------|------|------|
| API 契约 | 路由资源前缀（`resource_groups`） | controller/module、HTTP method | `order` 18 个端点、`member` 24 个 |
| 公开接口契约 | `module`（包/模块） | `class` → 方法 | `harness_framework.contracts` 10 个、`adaptive_control` 8 个 |
| 服务 | 成员（member） | — | `mall`、`harness_framework` |
| 跨服务 | Graph View + 调用边 | caller/callee 服务对 | `mall → mall-admin-web` |

规则分组必须与契约分组一致；**调用链不作为分组键**，只用于：

- 收集接口内部节点的原子规则（沿调用树）；
- 建立跨服务规则组合时的 caller/callee 关系；
- 影响分析（改一条规则会影响哪些接口/服务）。

### 2.2 分组元数据

```json
{
  "group_key": "order",
  "surface": "api",
  "kind": "resource",
  "label": "订单",
  "interface_count": 18,
  "rule_count": 42,
  "shared_rule_count": 9,
  "conflict_count": 2,
  "parent_service": "mall"
}
```

公开接口组同理，`surface = "interface"`、`kind = "module"`，`group_key` 用点分模块名。

---

## 3. 规则模型

### 3.1 原子规则（Rule Atom）

原子规则是聚合的最小单位，来自一个接口/节点：

```json
{
  "atom_id": "sha1:...",
  "snapshot_id": "...",
  "surface": "api",
  "subject_key": "GET|/order/list|OrderController.list",
  "group_key": "order",
  "text": "订单列表只返回当前登录用户有权限查看的订单",
  "rule_type": "authorization",
  "condition": "userId != null",
  "failure_mode": "返回空列表",
  "evidence": {
    "node_key": "snapshot::method:...",
    "file": "OrderController.java",
    "lines": [42],
    "interface": "GET /order/list",
    "call_path": ["list", "buildQuery", "checkMember"]
  },
  "origin": "aggregate_explanation.business_rules",
  "confidence": 0.8
}
```

`atom_id` 由 `(snapshot_id, subject_key, normalized_text, rule_type)` 哈希得到，
保证同一事实在不同快照/不同接口下可稳定去重与比较。

### 3.2 规则分类

沿用并扩展 `rule_type`：

| 类型 | 含义 | 聚合时的处理 |
|------|------|--------------|
| `validation` | 输入/数据校验 | 同条件合并；数值边界取交集并标冲突 |
| `authorization` | 权限/租户/角色 | 服务级通常会提升为全局不变量 |
| `workflow` | 状态/顺序/审批 | 跨接口聚合为流程规则 |
| `calculation` | 计算/折扣/限额 | 保留公式与上下界 |
| `transformation` | 数据转换/映射 | 多在接口级，少量上升为契约 |
| `consistency` | 数据一致性/幂等/重试 | 跨服务规则的主要来源 |
| `contract` | 请求/响应/状态码契约 | 跨服务规则的主要来源 |

### 3.3 规则集（Rule Set）

每一层是一个 `rule_set`：

```json
{
  "set_id": "service:mall:v1",
  "level": "service",
  "group_key": null,
  "parent_id": null,
  "version": 1,
  "digest": "sha256:...",
  "items": [
    {
      "atom_id": "sha1:...",
      "role": "shared",
      "source_sets": ["resource:order", "resource:cart"],
      "confidence": 0.9
    }
  ],
  "conflicts": [],
  "coverage": {"interfaces": 246, "interfaces_with_rules": 180}
}
```

`role`：

- `local`：只属于当前分组/接口；
- `shared`：来自 ≥2 个子分组，提炼为分组/服务级规则；
- `inherited`：从父层下发的全局不变量；
- `derived`：LLM 归纳出的更高层规则（必须带 `source_atom_ids`）。

---

## 4. 分层聚合语义

### 4.1 L1 接口级规则

输入：一个接口（API 端点或公开接口）的调用链规则。

处理：

1. 从 API 解释快照取 `aggregate_explanation.business_rules`；
2. 从公开接口的节点规则（`NodeRuleService`）沿调用树收集；
3. 归一化文本、去重、补 `rule_type`；
4. 保留 `source_node` / `evidence_lines` / `call_path`。

输出：`interface_rules[interface_key] = [atom...]`。

### 4.2 L2 分组级规则

输入：同组接口的原子规则。

处理：

1. **精确去重**：`normalized_text + rule_type` 相同 → 合并为一个 atom，累计 `source_interfaces`；
2. **近邻合并**：文本相似度 ≥ 阈值（默认 0.85）且 `rule_type` 相同 → 候选合并；
   - 第一阶段只标记 `merge_candidate`，不自动合并；
   - 第二阶段由 LLM 合并，必须输出 `source_atom_ids`，否则丢弃；
3. **提升共享规则**：出现在 ≥2 个接口的规则标记 `role = shared`；
4. **冲突检测**：
   - 同 `condition` 不同 `failure_mode`；
   - 同一主体/字段的数值边界矛盾（`> 0` vs `>= 0`、上限不同）；
   - 授权规则互相否定（允许 vs 拒绝同一角色）；
   - 状态迁移冲突（同一状态允许/禁止同一操作）。
5. **覆盖率**：`interfaces_with_rules / interface_count`。

输出：`group_rules[group_key]`，含 `local` / `shared` 与 `conflicts`。

### 4.3 L3 服务级规则

输入：该服务所有分组的规则集。

处理：

1. 跨组去重：同一条规则出现在多个组 → 服务级 `shared`；
2. **不变量提炼**：在 ≥50% 分组出现的授权/一致性规则，标记为 `service_invariant`；
3. **特化关系**：组规则是服务规则的更具体版本（相同主体、更窄条件）→ 保留父子关系；
4. **冲突升级**：组间冲突在服务级必须显式列出，不能取其一；
5. **LLM 归纳**（可选）：输入 `group_rules`（带 id），输出服务级规则，
   每条必须引用 `source_rule_ids`，否则拒绝。

输出：`service_rules[service]`，含：
- `invariants`：服务级不变量；
- `capability_rules`：按业务能力（分组）组织的规则；
- `conflicts`：组间冲突；
- `coverage`：按分组/接口统计。

### 4.4 L4 跨服务规则

输入：

- caller 服务的服务级规则；
- callee 服务的服务级规则；
- 拓扑调用边（`TopologyArtifact` / `graph_views`）；
- 跨服务调用流程（`flow`）。

跨服务规则分四类：

| 类别 | 说明 | 示例 |
|------|------|------|
| `contract` | 请求/响应/状态码/版本契约 | caller 只接受 `code=0`，callee 返回 `code=200` |
| `consistency` | 幂等、重试、顺序、事务边界 | caller 重试 3 次，callee 非幂等 |
| `security` | 认证/租户/权限透传 | caller 透传 tenantId，callee 未校验 |
| `budget` | 超时、限流、重试预算 | caller 超时 1s，callee P99 2s |

处理：

1. 对每条跨服务调用边，取 caller 端点的 `integration_contracts` 与 callee 端点的契约规则；
2. 匹配同一操作（HTTP method + path / RPC method / topic）；
3. 生成**义务（obligation）**、**假设（assumption）**、**错配（mismatch）**；
4. 跨多条边的规则（如全局租户透传）提升为 `cross_service_invariant`。

输出：`cross_service_rules[view_id]`，按调用边组织，附双方证据。

---

## 5. 聚合算法

### 5.1 规则归一化

```text
normalize(text):
  - Unicode NFKC
  - 去首尾空白、合并连续空白
  - 去除末尾标点
  - 统一中英文标点/数字/比较符（>= / ≥）
  - 保留业务名词与条件，不做摘要
```

`normalized_text` 用于精确去重；**原文**保留展示。

### 5.2 去重与合并优先级

```text
精确相同      → 自动合并
相似 ≥ 0.85   → 标记候选，不自动合并
相似 0.70–0.85 → 仅提示
相似 < 0.70   → 不合并
```

LLM 合并的硬约束：

- 输入必须带 `atom_id`；
- 输出每条规则必须带 `source_atom_ids`（非空）；
- 不得引入输入中不存在的实体/条件/数值；
- 不确定时输出 `uncertainties`，不合并。

### 5.3 冲突检测

冲突不是错误，而是需要人确认的业务决策点。冲突对象：

```json
{
  "conflict_id": "sha1:...",
  "kind": "numeric_bound|negation|state_transition|failure_mode",
  "subject": "订单金额",
  "atom_a": "sha1:a",
  "atom_b": "sha1:b",
  "detail": "a 要求 > 0，b 允许 = 0",
  "status": "open|resolved|accepted"
}
```

服务级/跨服务级冲突必须保持 `open`，直到人工在 UI 标记 `resolved` 或 `accepted`。

### 5.4 置信度

```text
confidence = min(1, 0.5 * evidence_strength + 0.3 * source_count + 0.2 * level_weight)
```

- `evidence_strength`：有源码行号/节点 = 1，仅有文本 = 0.5；
- `source_count`：出现在多少个独立接口/分组；
- `level_weight`：接口 0.6、分组 0.8、服务 0.9、跨服务 1.0。

置信度只用于排序/过滤，不删除规则。

---

## 6. 数据模型与存储

新增表（与现有 `snapshot_rule_*` 并存，避免破坏节点规则）：

```sql
CREATE TABLE rule_atoms (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL,
  surface TEXT NOT NULL,              -- api | interface
  subject_key TEXT NOT NULL,          -- api_key / interface symbol
  group_key TEXT NOT NULL,
  text TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  rule_type TEXT NOT NULL,
  condition TEXT,
  failure_mode TEXT,
  origin TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE rule_sets (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL,
  level TEXT NOT NULL,                -- interface | group | service | cross_service
  surface TEXT,
  group_key TEXT,
  parent_id TEXT,
  version INTEGER NOT NULL,
  digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE rule_set_items (
  set_id TEXT NOT NULL REFERENCES rule_sets(id) ON DELETE CASCADE,
  atom_id TEXT NOT NULL REFERENCES rule_atoms(id) ON DELETE RESTRICT,
  role TEXT NOT NULL,                 -- local | shared | inherited | derived
  source_sets_json TEXT,
  confidence REAL NOT NULL,
  PRIMARY KEY(set_id, atom_id)
);

CREATE TABLE rule_conflicts (
  id TEXT PRIMARY KEY,
  repository_snapshot_id TEXT NOT NULL,
  level TEXT NOT NULL,
  group_key TEXT,
  kind TEXT NOT NULL,
  subject TEXT,
  atom_a TEXT NOT NULL,
  atom_b TEXT NOT NULL,
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);

CREATE TABLE cross_service_rules (
  id TEXT PRIMARY KEY,
  view_id TEXT NOT NULL,
  caller_service TEXT NOT NULL,
  callee_service TEXT NOT NULL,
  relation TEXT NOT NULL,             -- contract | consistency | security | budget
  rule_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
```

`rule_sets.digest` 由 `(level, group_key, 输入 atom ids 排序, prompt_digest)` 计算，
用于判断是否需要重新合成。

---

## 7. 对外能力

### 7.1 HTTP

```http
GET /api/rule-map?snapshot_id=<id>
```

Phase 1 一次返回 interface / group / service / integration 四个投影，前端切换视图不再请求；
conflicts 与 cross-service 组合接口在 Phase 2/4 增加。

返回：

```json
{
  "repository_snapshot_id": "...",
  "level": "group",
  "groups": [
    {
      "group_key": "order",
      "interface_count": 18,
      "rules": [
        {
          "atom_id": "...",
          "text": "...",
          "rule_type": "authorization",
          "role": "shared",
          "source_interfaces": ["GET /order/list", "POST /order/cancel"],
          "evidence": [{"file": "...", "lines": [42]}]
        }
      ],
      "conflicts": []
    }
  ]
}
```

### 7.2 CLI

```bash
codeevolution rule-map --snapshot-id <id>                 # 按资源分组输出 Markdown
codeevolution rule-map --snapshot-id <id> --level interface
codeevolution rule-map --snapshot-id <id> --format json -o rules.json
codeevolution rule-map --snapshot-id <id> --server http://127.0.0.1:8765
```

### 7.3 Web 控制台

知识中心新增「业务规则地图」：

- 左树：服务 → 分组 → 接口 → 规则；
- 右侧：规则详情（原文、类型、条件、失败模式、证据源码、来源接口链）；
- 顶部：覆盖率、共享规则数、冲突数；
- 冲突面板：并排展示冲突双方，支持 `resolved / accepted`；
- 跨服务页签：按调用边展示 contract/consistency/security/budget 规则与错配。

---

## 8. 分阶段实施

### Phase 1：规则地图（确定性）— 已实现

- `application/rule_map_service.py`：从 API 解释快照收集接口级规则与集成契约；
- 按 `resource_groups` 分组，精确去重，输出 interface / group / service 三级；
- `GET /api/rule-map`、`codeevolution rule-map`、知识中心「业务规则地图」；
- 不调用 LLM，不写库（只读计算）；每条规则带 `atom_id`、来源接口、文件与源码行；
- 实测：codehistory 82 个端点、73 个有规则、158 条规则、23 个资源组、104 条集成契约
  （其中 22 条 HTTP/RPC/消息跨服务候选），聚合耗时约 0.09s。

### Phase 2：分组规则质量

- 接入公开接口规则（`NodeRuleService` + 调用树）；
- 近邻合并候选、冲突检测；
- 规则原子与规则集落库；digest 稳定；
- LLM 合并提示词（带 `source_atom_ids` 硬约束）。

### Phase 3：服务级规则

- 跨组去重、不变量提炼、服务级 LLM 归纳；
- Web 规则地图（树 + 详情 + 冲突面板）；
- 服务级规则导出 Markdown。

### Phase 4：跨服务规则

- 结合 `graph_views` / `TopologyArtifact` / `flow`；
- 生成 contract/consistency/security/budget 四类规则与错配；
- 跨服务规则地图与导出；
- 人工确认状态（open/resolved/accepted）。

---

## 9. 验收指标

| 指标 | 目标 |
|------|------|
| 可追溯率 | 100% 聚合规则能回到 `atom_id` 与源码行 |
| 覆盖率 | 有规则的接口数 / 接口总数，按分组展示 |
| 去重率 | 分组内重复规则合并比例 |
| 冲突识别 | 数值边界/否定/状态迁移/失败模式四类冲突可检出 |
| 合成约束 | LLM 输出 100% 带 `source_atom_ids`，否则拒绝入库 |
| 复现性 | 同输入/同提示词版本 digest 一致 |
| 性能 | 246 端点、约 1k 规则的聚合 < 2s（不含 LLM） |

---

## 10. 风险与边界

1. **规则文本质量依赖上游 LLM**：接口解释不稳定时，聚合结果也会波动；用 digest/版本记录。
2. **近邻合并误判**：语义相近但业务不同的规则可能被合并；第一阶段只标记候选，人工确认。
3. **冲突误报**：不同上下文下的规则看起来矛盾（如不同租户策略）；必须保留上下文与证据。
4. **跨服务规则证据不足**：拓扑边可能缺失；输出 `assumption` 而不是断言。
5. **规则爆炸**：服务级规则过多时按 `rule_type + group` 分层展示，不强行压缩。
6. **隐私/权限**：规则可能包含业务敏感信息，导出需权限控制（沿用现有 API Key/快照权限）。

---

## 11. 与现有能力的关系

| 现有能力 | 在规则分层中的角色 |
|----------|--------------------|
| API 契约 | 接口身份、资源分组、端点证据 |
| 公开接口契约 | 非 HTTP 接口身份、模块分组、调用树根 |
| API 解释快照 | L1 原子规则的主要来源（`business_rules`） |
| `NodeRuleService` | 调用链节点级规则，补充 L1 |
| `business_rules` LLM section | 规则类型/条件/失败模式的补充来源 |
| 模块拓扑/业务分类 | 语义分组与能力视图 |
| 统一服务拓扑/流程追踪 | L4 跨服务规则的调用边与证据 |

---

## 12. 结论

规则分层的关键不是「把所有规则拼在一起」，而是：

1. **接口级原子化**：每条规则有稳定 id、类型、条件、证据；
2. **分组级归并**：精确去重 + 共享规则 + 冲突显式；
3. **服务级提炼**：不变量、特化关系、覆盖率；
4. **跨服务级组合**：沿调用边生成义务/假设/错配。

调用链用于**收集证据和建立跨服务关系**，分组仍按契约身份（API 资源前缀 / 接口模块）
进行，避免把无关业务通过共享工具函数合并到一起。
