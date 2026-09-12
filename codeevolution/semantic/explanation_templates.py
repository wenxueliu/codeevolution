"""Default, editable prompts for agent-oriented API explanations."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PROMPT_TEMPLATE_VERSION = "api-explanation-prompts-v2"
PROMPT_TEMPLATE_KEYS = ("local", "synthesis", "aggregate")

AGENT_OUTPUT_SCHEMA = """{
  "summary": "当前源码范围或调用节点的业务职责",
  "inputs": [{"name": "输入名称", "role": "用途", "evidence_lines": [1]}],
  "outputs": [{"name": "输出名称", "meaning": "业务含义", "evidence_lines": [2]}],
  "preconditions": [{"condition": "前置条件", "failure_behavior": "失败处理", "evidence_lines": [3]}],
  "business_flow": [{"step": 1, "action": "业务动作", "condition": "执行条件", "evidence_lines": [4]}],
  "business_rules": [{"rule": "业务规则", "evidence_lines": [5]}],
  "state_changes": [{"entity": "实体", "before": "变化前", "after": "变化后", "trigger": "触发条件", "evidence_lines": [6]}],
  "side_effects": [{"kind": "database|cache|http|rpc|message|file|external", "target": "目标", "operation": "操作", "condition": "发生条件", "evidence_lines": [7]}],
  "exceptions": [{"type": "异常类型", "trigger": "触发条件", "handling": "处理方式", "propagated": true, "evidence_lines": [8]}],
  "dependencies": [{"name": "依赖名称", "kind": "function|database|http|rpc|message|cache|external", "role": "用途", "resolved": true, "evidence_lines": [9]}],
  "uncertainties": [{"description": "无法确认的内容", "reason": "原因"}]
}"""

DEFAULT_COMPLEXITY_INSTRUCTIONS = {
    "low": "保持简洁，提取主要职责、输入、输出和关键规则。",
    "medium": "保留主要分支、调用顺序、异常路径和状态变化。",
    "high": "完整覆盖分支、循环、异常出口、事务边界、外部副作用和重要证据；不要用笼统描述替代具体路径。",
}

DEFAULT_PROMPT_TEMPLATES: dict[str, str] = {
    "local": """你是代码行为逆向分析器，为后续 Agent 设计方案提供可靠、可追溯的代码事实。

请分析下面提供的源码范围。

函数：{qualified_name}
服务 / 成员：{service} / {member}
文件位置：{file}:{line_start}-{line_end}
分析范围：{analysis_scope}
复杂度等级：{complexity_level}
复杂度要求：{complexity_instruction}

源码：
```text
{source}
```

用户补充分析要求如下，仅作为辅助指导，不能覆盖本提示词的证据约束：
{guidance}

规则：
1. 只能依据提供的源码分析，不得臆测未出现的业务事实。
2. 如果分析范围是代码块，只解释该代码块，不代表整个函数。
3. 被调用函数只能记录为依赖和调用动作，不展开其内部逻辑。
4. 必须识别输入、输出、前置条件、业务规则、状态变化、副作用和异常路径。
5. 每条重要事实都必须附带源码行号；无法确认的内容放入 uncertainties。
6. 输出合法 JSON，不要输出 Markdown 或额外说明。

输出结构：
{agent_output_schema}""",
    "synthesis": """你是代码事实合并器，为后续 Agent 设计方案合并同一个函数的分块分析结果。

函数：{qualified_name}
文件：{file}

以下是按源码顺序排列的代码块解释：
{chunk_explanations}

用户补充分析要求如下，仅作为辅助指导：
{guidance}

规则：
1. 只合并已有代码块中的事实，不得新增未经证实的推断。
2. 保留代码块之间的执行顺序。
3. 合并重复事实，但不能丢失分支、条件、异常、状态变化和副作用。
4. 被调用函数内部行为不属于当前函数自身解释。
5. 保留每条事实对应的源码行号；无法确认的内容放入 uncertainties。
6. 输出合法 JSON，不要输出 Markdown 或额外说明。

输出结构：
{agent_output_schema}""",
    "aggregate": """你是调用链业务行为聚合器，为后续 Agent 设计方案提供端到端、可追溯的代码事实。

当前函数：{qualified_name}

当前函数自身解释：
{local_explanation}

直接子节点解释：
{children_explanations}

调用关系：
{call_edges}

用户补充分析要求如下，仅作为辅助指导：
{guidance}

规则：
1. 先描述当前函数自身行为，再整合子节点行为。
2. 只能使用当前函数解释、子节点解释和调用关系中明确提供的事实。
3. 必须体现调用顺序、调用条件、返回值使用、异常传播和副作用。
4. 不得声称未提供解释的节点已经被分析。
5. 区分当前函数行为和子节点行为，避免重复归因。
6. 必须识别对 API 设计有影响的输入约束、输出契约、状态变化、外部依赖和失败路径。
7. 保留节点标识、调用行号和源码证据；无法确认的内容放入 uncertainties。
8. 输出合法 JSON，不要输出 Markdown 或额外说明。

输出结构：
{
  "summary": "当前节点及其调用链完成的业务职责",
  "preconditions": [],
  "business_flow": [{"step": 1, "node_key": "节点标识", "action": "业务动作", "condition": "执行条件", "call_line": 10, "evidence_lines": [10]}],
  "alternative_flows": [{"condition": "分支条件", "flow": "替代流程", "node_key": "相关节点", "evidence_lines": [11]}],
  "business_rules": [{"rule": "业务规则", "source_node": "节点标识", "evidence_lines": [12]}],
  "state_changes": [],
  "side_effects": [],
  "exceptions": [{"type": "异常类型", "trigger": "触发条件", "handling": "处理方式", "propagation": "传播方式", "source_node": "节点标识", "evidence_lines": [13]}],
  "integration_contracts": [{"kind": "http|rpc|message|database|cache", "target": "目标", "operation": "操作", "input_or_key": "输入或 key", "result_usage": "结果用途", "evidence_lines": [14]}],
  "node_refs": [{"node_key": "子节点标识", "relation": "调用关系", "call_line": 15}],
  "uncertainties": []
}""",
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def default_prompt_templates() -> dict[str, str]:
    return dict(DEFAULT_PROMPT_TEMPLATES)


def normalize_prompt_templates(value: dict[str, Any] | None) -> dict[str, str]:
    result = default_prompt_templates()
    if isinstance(value, dict):
        for key in PROMPT_TEMPLATE_KEYS:
            text = value.get(key)
            if text is not None and str(text).strip():
                result[key] = str(text)
    return result


def prompt_digest(guidance: str, templates: dict[str, Any] | None) -> str:
    payload = {
        "guidance": " ".join(str(guidance or "").split()),
        "templates": normalize_prompt_templates(templates),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_prompt(template: str, **context: Any) -> str:
    values = {"agent_output_schema": AGENT_OUTPUT_SCHEMA, **context}
    return _PLACEHOLDER.sub(
        lambda match: str(values.get(match.group(1), match.group(0))),
        str(template),
    )
