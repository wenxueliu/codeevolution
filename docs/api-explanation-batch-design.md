# API 功能解释：自定义提示词与批量生成最终设计方案

> 状态：最终设计方案
>
> 关联设计：[API 功能解释与快照设计](api-explanation-snapshot-design.md)
>
> 验收范围：仅 UI 自动化验收，不要求新增 UT。

## 1. 方案结论

本方案解决三个问题：

1. API 解释支持自定义提示词，并可一键应用到当前 Snapshot 的全部端点；
2. 服务级批量生成全部端点的 API 解释；
3. 批量任务支持持久化进度、失败重试、刷新恢复和完成统计。

最终采用以下决策：

| 决策项 | 最终方案 |
|---|---|
| 提示词作用域 | 当前 `repository_snapshot_id` 级别 |
| 提示词修改 | 每次保存产生不可变新版本，不修改历史解释 |
| 一键全量修改 | 保存新版本后直接创建一次全量批量任务 |
| 批量范围 | 当前页面选中的单个 Repository Snapshot |
| 多成员逻辑服务 | 第一阶段不跨成员合并，后续单独扩展 |
| 批量任务存储 | 复用 `api-explanations.db`，与解释快照放在同一持久化边界 |
| 批量执行 | 持久化 Job/Item + 有界线程池 |
| 默认并发 | 2，允许配置 1～5 |
| 失败重试 | 创建可追溯的新重试任务，只重试失败端点 |
| 历史结果 | 保留旧解释，成功的新解释完成后才更新当前指针 |
| 验收方式 | UI 自动化，不新增 UT |

## 2. 背景与现状

当前 API 功能解释以端点为单位手动触发：一个端点对应一棵调用树和一个 `ExplanationSnapshot`。

现有实现已经具备：

- Snapshot 级源码和 CodeGraph 证据读取；
- 端点调用树冻结；
- 叶子节点到入口节点的解释生成；
- `pending/running/completed/partial/failed` 快照状态；
- 当前解释快照指针和历史快照管理。

当前缺口：

- API 解释提示词已支持 Snapshot 级统一业务指导和三段可编辑模板；
- 一个服务的多个端点必须逐个点击；
- 没有批量 Job 和端点级 Item；
- 没有批量进度、失败原因和统一重试入口。

## 3. 领域模型

### 3.1 提示词配置 `ApiExplanationPromptProfile`

提示词配置表示某个 Snapshot 可使用的一版 API 解释指导。配置版本不可变，编辑操作实际是创建新版本。

建议存储在现有 `api-explanations.db`：

| 字段 | 说明 |
|---|---|
| `id` | 提示词配置 ID |
| `repository_snapshot_id` | 所属 Repository Snapshot |
| `prompt_text` | 用户自定义统一业务分析指导 |
| `prompt_templates` | `local`、`synthesis`、`aggregate` 三段可编辑系统模板 JSON |
| `version` | Snapshot 内递增版本号 |
| `prompt_digest` | 规范化内容的 SHA-256 摘要 |
| `created_at` | 创建时间 |
| `created_by` | 创建来源，默认 `web` |
| `is_current` | 是否为当前默认版本 |

唯一约束：

```text
(repository_snapshot_id, version)
(repository_snapshot_id, prompt_digest)
```

### 3.2 批量任务 `ApiExplanationBatchJob`

| 字段 | 说明 |
|---|---|
| `id` | 批量任务 ID |
| `repository_snapshot_id` | 目标 Snapshot |
| `prompt_profile_id` | 使用的提示词版本 |
| `prompt_digest` | 创建任务时冻结的提示词摘要 |
| `status` | `queued/running/completed/partial/failed/cancelled` |
| `total_count` | 端点总数 |
| `completed_count` | 已完成数量 |
| `failed_count` | 失败数量 |
| `skipped_count` | 跳过数量 |
| `running_count` | 执行中数量 |
| `cancel_requested` | 是否请求取消 |
| `retry_of_job_id` | 来源重试任务，可为空 |
| `requested_at` | 创建时间 |
| `started_at` | 开始时间 |
| `completed_at` | 完成时间 |
| `error_message` | 任务级错误 |

### 3.3 批量任务项 `ApiExplanationBatchItem`

| 字段 | 说明 |
|---|---|
| `batch_id` | 所属批量任务 |
| `endpoint_key` | `METHOD + PATH + HANDLER` |
| `method` | HTTP 方法 |
| `path` | API 路径 |
| `handler` | 处理函数 |
| `status` | `queued/running/completed/failed/skipped/cancelled` |
| `attempt_count` | 当前端点执行次数 |
| `explanation_snapshot_id` | 成功生成的解释快照 |
| `skip_reason` | 跳过原因 |
| `error_message` | 端点失败原因 |
| `started_at` | 开始时间 |
| `completed_at` | 完成时间 |

### 3.4 与解释快照的关系

批量 Item 不直接保存解释正文，只保存生成结果对应的 `explanation_snapshot_id`。

```text
Batch Job
  └── Batch Item
        └── Explanation Snapshot
```

单端点生成和批量生成均调用现有 `ExplanationGenerationService`，保证提示词合成、源码冻结、覆盖校验和发布规则一致。

## 4. 自定义提示词设计

### 4.1 提示词合成规则

提示词配置包含一份统一业务指导和三段按职责区分的系统模板：

- `local`：完整函数或单个代码块的自身解释；
- `synthesis`：多个代码块合并为当前函数自身解释；
- `aggregate`：当前节点与直接子节点的调用链聚合，根节点与中间节点共用。

模板支持 `{qualified_name}`、`{source}`、`{local_explanation}`、`{children_explanations}`、`{guidance}` 等变量。用户提示词不覆盖系统约束，而是作为“业务分析指导”注入模板：

```text
系统约束：
- 只能根据提供的源码和调用链分析
- 不得臆测未提供的信息
- 必须输出规定的 JSON 结构
- 对无法确认的内容明确标记为未知

用户补充分析要求：
{prompt_text}

当前端点：
{method} {path}
处理函数：
{handler}

调用链与源码：
{evidence}

请输出 JSON。
```

统一业务指导同时作用于：

1. 源码块解释；
2. 当前函数本地解释；
3. 调用链聚合解释。

系统约束、JSON schema、源码证据和审计要求不能被用户提示词删除或覆盖。

默认输出面向后续 Agent 设计方案使用，必须保留输入/输出、前置条件、分支流程、业务规则、状态变化、外部副作用、异常传播、依赖和 uncertainties，并为重要事实记录源码行号或节点调用位置。复杂度只改变分析范围和完整性要求，不复制出按复杂度分别维护的模板。

### 4.2 提示词优先级

```text
单次请求 custom_prompt
    > 指定 prompt_profile_id
    > Snapshot 当前提示词版本
    > 系统默认提示词
```

生成解释快照时必须记录：

- `prompt_profile_id`；
- `prompt_version`；
- `prompt_digest`。

这样可以回答“这个解释由哪一版提示词生成”。

### 4.3 API

获取提示词版本：

```http
GET /api/api-explanation-prompts?repository_snapshot_id=<snapshot-id>
```

创建新版本并设为当前版本：

```http
POST /api/api-explanation-prompts
```

请求：

```json
{
  "repository_snapshot_id": "snapshot-1",
  "prompt_text": "重点关注订单状态变化、库存扣减和异常处理"
}
```

扩展单端点接口：

```http
POST /api/api-explanations/generate
```

增加：

```json
{
  "prompt_profile_id": "prompt-v3",
  "custom_prompt": ""
}
```

`custom_prompt` 不落为默认配置，只对本次端点生成生效。

### 4.4 “一键全量修改”的准确语义

“一键全量修改”不修改历史快照，而是执行以下动作：

```text
保存新提示词版本
      ↓
读取当前 Snapshot 的全部端点
      ↓
创建一个全量批量任务
      ↓
用新提示词生成新解释快照
      ↓
每个端点成功后更新当前解释指针
```

失败端点继续保留旧解释，不能因为新版本失败而清空原结果。

## 5. 服务级批量生成

### 5.1 批量范围

第一阶段批量边界是当前页面选中的单个 `repository_snapshot_id`，读取：

```text
snapshot.api_contract.endpoints
```

这里的“服务级”指当前 Snapshot 所代表的服务成员，不在第一阶段跨多个 Repository Member 合并。

端点唯一标识：

```text
METHOD + PATH + HANDLER
```

以下端点不进入模型执行队列，直接标记为 `skipped`：

- 缺少 `handler`；
- 无法解析源码位置；
- 端点信息不完整；
- 已经存在相同 `prompt_digest` 的完成解释快照。

### 5.2 API

创建批量任务：

```http
POST /api/api-explanations/batches
```

请求：

```json
{
  "repository_snapshot_id": "snapshot-1",
  "prompt_profile_id": "prompt-v3",
  "mode": "all",
  "concurrency": 2
}
```

返回：

```json
{
  "batch": {
    "id": "batch-1",
    "status": "queued",
    "total_count": 56,
    "completed_count": 0,
    "failed_count": 0,
    "skipped_count": 3
  }
}
```

查询任务：

```http
GET /api/api-explanations/batches/{batch_id}
```

返回批量摘要和端点明细：

```json
{
  "id": "batch-1",
  "status": "partial",
  "progress": {
    "total": 56,
    "completed": 51,
    "failed": 2,
    "skipped": 3,
    "running": 0,
    "percent": 91
  },
  "items": []
}
```

取消任务：

```http
POST /api/api-explanations/batches/{batch_id}/cancel
```

### 5.3 幂等规则

同一个 Snapshot、同一个提示词摘要下，只允许一个 `queued/running` 批量任务：

```text
repository_snapshot_id + prompt_digest + active status
=> 返回已有任务，不重复创建
```

批量任务内部按 `endpoint_key` 去重。

## 6. 批量执行与状态机

### 6.1 执行流程

```text
保存提示词版本
      ↓
点击“应用到全部端点”
      ↓
读取 Snapshot API 契约
      ↓
创建 Batch Job 和 Batch Items
      ↓
并发执行单端点解释任务
      ↓
每个端点生成独立 Explanation Snapshot
      ↓
成功后更新该端点当前解释指针
      ↓
更新批量任务统计
```

### 6.2 批量状态

```text
queued
  ↓
running
  ├── completed
  ├── partial
  ├── failed
  └── cancelled
```

- `completed`：所有可执行端点成功；
- `partial`：至少一个端点成功，同时存在失败或跳过；
- `failed`：没有端点成功，或任务初始化失败；
- `cancelled`：用户取消，未执行端点转为 `cancelled`。

单个端点状态：

```text
queued → running → completed
                 ├→ failed
                 ├→ skipped
                 └→ cancelled
```

单个端点失败不阻塞其他端点。

### 6.3 并发控制

默认并发数为 `2`，允许配置范围 `1～5`。批量任务共享全局 LLM 并发限制，避免多个任务同时运行时超过模型服务限流。

第一阶段使用进程内有界线程池，但 Job 和 Item 状态必须持久化。服务重启后：

- `queued` 项继续执行；
- `running` 项恢复为 `queued`，避免永久卡住；
- 已完成和已失败项保持原状态。

## 7. 失败、重试与取消

### 7.1 自动重试

以下错误允许自动重试最多 2 次：

- LLM 超时；
- 网络错误；
- 限流；
- 临时服务错误。

等待时间建议为 2 秒、5 秒。

### 7.2 不自动重试

以下错误不自动重试：

- Snapshot 源码缺失；
- handler 无法解析；
- 调用链为空；
- Snapshot 已删除；
- 提示词校验失败。

这些错误需要用户修复 Snapshot 或提示词后手动重试。

### 7.3 失败重试模型

重试不修改原批量任务。点击“重试失败”时创建新的子任务：

```text
Batch Job A：56 个端点，2 个失败
      ↓ retry failed
Batch Job B：只包含 2 个失败端点
      └── retry_of_job_id = A
```

好处：

- 原任务统计保持稳定；
- 每次重试可审计；
- 可以清晰区分初次运行和重试结果；
- 不会重复执行已完成端点。

### 7.4 取消规则

- `queued` Item 立即转为 `cancelled`；
- `running` Item 不强杀当前模型调用，当前调用结束后停止后续任务；
- 已完成 Item 不回滚；
- 任务最终状态为 `cancelled` 或 `partial`，取决于是否已有成功结果。

## 8. 页面设计

### 8.1 API 解释设置

在 API 契约区域增加“API 解释设置”面板：

- 当前提示词版本；
- 提示词编辑框；
- 字数限制和保存按钮；
- 历史版本列表；
- “保存并应用到全部端点”按钮；
- 当前 Snapshot 端点数量；
- 模型调用费用提示。

保存按钮只创建提示词版本，不调用模型。

“保存并应用到全部端点”执行保存和批量任务创建两个动作，并弹窗确认：

```text
将为 56 个 API 端点生成解释，预计产生模型调用费用。
已有相同提示词版本的完成结果将自动复用。
是否继续？
```

### 8.2 批量进度卡片

```text
API 解释批量任务进行中                         91%

已完成 51 / 56
失败 2 · 跳过 3 · 执行中 0

[查看端点明细] [重试失败] [取消任务]
```

显示：

- 总端点数；
- 已完成数；
- 执行中数；
- 失败数；
- 跳过数；
- 取消数；
- 百分比；
- 当前提示词版本；
- 当前执行端点。

页面刷新后通过批量任务查询接口恢复状态，不重新创建任务。

### 8.3 端点明细

| 端点 | 状态 | 解释快照 | 错误 | 操作 |
|---|---|---|---|---|
| `POST /orders` | 已完成 | snapshot-1 | - | 查看 |
| `GET /orders/{id}` | 失败 | - | 源码缺失 | 重试 |
| `DELETE /orders/{id}` | 跳过 | - | 缺少 handler | - |

完成端点可以直接打开对应的解释快照；失败端点显示后端错误详情；跳过端点显示 `skip_reason`。

## 9. 历史结果与一致性

批量任务不能直接覆盖旧解释：

```text
旧提示词版本
  └── Explanation Snapshot A，保持不变

新提示词版本
  └── Explanation Snapshot B，成功后成为当前版本
```

如果批量任务部分失败：

- 成功端点切换到新的解释快照；
- 失败端点继续保留旧解释；
- 用户可以单独重试失败端点；
- 当前端点解释查询始终返回最近一次成功发布的结果。

## 10. 实施阶段

### Phase 1：提示词版本和单端点支持

- 在 `ExplanationSnapshotStore` 中增加提示词版本存储；
- 增加提示词读取和创建接口；
- 单端点生成支持 `prompt_profile_id` 和 `custom_prompt`；
- 解释快照记录提示词版本和摘要；
- 增加提示词编辑 UI。

### Phase 2：批量任务

- 在 `api-explanations.db` 增加 Batch Job / Batch Item 表；
- 增加批量创建、查询和取消接口；
- 复用现有 `ExplanationGenerationService`；
- 增加有界并发和幂等处理；
- 增加批量进度 UI。

### Phase 3：失败重试与恢复

- 增加失败端点子任务重试；
- 增加服务重启后的任务恢复；
- 增加端点级错误、耗时和模型调用统计；
- 增加批量历史任务查询。

第一阶段不实现跨多个 Repository Member 的逻辑服务批量任务，也不实现独立分布式 Worker。

## 11. UI 自动化验收方案

本功能不新增 UT，验收全部通过 Web UI 自动化完成。

### UI-API-001：保存自定义提示词

1. 进入指定 Snapshot 的知识中心；
2. 打开“API 解释设置”；
3. 输入自定义提示词并保存；
4. 验证页面显示新版本号；
5. 刷新页面，验证提示词版本仍然存在。

### UI-API-002：保存并全量应用提示词

1. 输入新提示词；
2. 点击“保存并应用到全部端点”；
3. 在确认弹窗中继续；
4. 验证只创建一个批量任务；
5. 验证任务显示 Snapshot、提示词版本和端点总数。

### UI-API-003：批量进度和完成统计

1. 启动批量任务；
2. 验证显示进度百分比；
3. 验证显示已完成、执行中、失败、跳过数量；
4. 验证端点明细状态会从 `queued` 变为 `running`、`completed` 或 `failed`；
5. 全部完成后验证完成端点可以打开解释快照。

### UI-API-004：刷新页面恢复任务

1. 批量任务执行中刷新页面；
2. 验证不会重新创建批量任务；
3. 验证仍显示原任务 ID、原提示词版本和当前进度；
4. 验证任务最终统计与刷新前一致。

### UI-API-005：失败端点重试

1. 构造至少一个端点失败的批量任务；
2. 验证失败端点显示错误原因；
3. 点击“重试失败”；
4. 验证创建新的重试任务，且只包含失败端点；
5. 验证原批量任务保持原统计；
6. 验证重试成功后端点出现新的解释快照。

### UI-API-006：取消批量任务

1. 启动批量任务；
2. 点击“取消任务”；
3. 验证排队端点转为取消状态；
4. 验证已完成端点结果不消失；
5. 验证任务最终显示 `cancelled` 或 `partial`。

### UI-API-007：历史结果保留

1. 先生成一版 API 解释；
2. 修改提示词并执行全量任务；
3. 验证成功端点切换到新解释；
4. 验证历史解释快照仍可查看；
5. 验证失败端点仍保留旧解释。

## 12. 非目标

本期不包含：

- 跨多个 Repository Member 的统一服务批量任务；
- 实时 WebSocket 推送，第一阶段使用轮询；
- 修改已发布 Explanation Snapshot 的正文；
- 自动修改用户提示词；
- 分布式 Worker 和跨实例任务抢占；
- 新增 UT 或后端单元测试验收。
