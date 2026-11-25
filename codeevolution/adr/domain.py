"""ADR generation domain: source kinds, prompt templates and MADR rendering.

The generation subsystem turns model output into MADR 4.0 decision records.
Everything here is pure: prompts are resolved by the service, model calls are
injected, and rendering is a function of the parsed decision object, so the
same inputs always produce the same Markdown.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

SOURCE_KINDS = ("commit", "file", "text")
HISTORY_KIND = "history"
PROMPT_SOURCE_KINDS = SOURCE_KINDS  # history reuses the commit prompt
DEFAULT_STATUS = "proposed"
MAX_TITLE_CHARS = 120

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# The JSON contract every prompt asks the model to satisfy.  It is also the
# schema the parser tolerates: unknown keys are ignored, missing keys render
# as explicit TODOs instead of failing the whole generation.
DECISION_SCHEMA: dict[str, Any] = {
    "is_decision": True,
    "reason": "为什么这段输入构成（或不构成）架构决策",
    "title": "一句话标题，说明问题与选中的方案",
    "slug": "kebab-case-ascii-slug",
    "context": "背景与问题陈述（2-4 句）",
    "drivers": ["推动决策的质量属性、约束或力量"],
    "options": [
        {"name": "被考虑的选项", "pros": ["优点"], "cons": ["缺点"]}
    ],
    "chosen_option": "最终选中的选项名称",
    "outcome": "选择理由与关键取舍",
    "consequences": {"good": ["正面后果"], "bad": ["代价或权衡"]},
    "confirmation": "如何确认决策已被执行（测试、监控或评审）",
    "confidence": 0.0,
    "evidence": [{"path": "文件路径", "line": 1, "note": "该行如何支撑结论"}],
}

# Default prompt per source kind.  Users override it per service through the
# prompt API/CLI; the default is only the fallback.
_COMMON_RULES = """\
判断规则（只依据给出的材料，不得臆测）：
判断标准：
1. 是否改变模块边界、外部契约、数据模型、部署拓扑、关键技术选型或长期约束；
2. 是否做出需要后续维护者知晓的取舍（有被放弃的替代方案）；
3. 单纯的样式、文案、依赖升级、测试补充、格式化不算架构决策。
输出约束：
- options 必须是同一决策下互斥的候选方案，互补措施合并成一个方案，不要拆成两个选项；
- evidence.path 使用仓库相对路径，不要写绝对路径；没有把握就降低 confidence，不要编造证据；
- 用中文填写，title 不超过 60 个字符。
只输出一个 JSON 对象，不要 Markdown 代码块，不要解释。字段契约：
{{schema}}
"""

DEFAULT_PROMPTS: dict[str, str] = {
    "commit": """\
你是资深架构师，负责从一次 git 提交中抽取架构决策记录（ADR）。

服务：{{service}}
仓库：{{repo}}
提交：{{source_ref}}
作者：{{author}}
时间：{{committed_at}}
提交信息：
{{commit_message}}

变更文件：
{{commit_files}}

变更内容（可能被截断）：
{{diff}}

已有 ADR 标题（避免重复记录同一个决策）：
{{existing_adrs}}

""" + _COMMON_RULES + """
其它要求：
- title 用中文描述“问题 + 方案”，不要照抄 commit message；
- slug 用英文小写连字符，最长 60 字符；
- evidence 必须引用上面出现过的文件路径（有行号就填行号）；
- 如果不是架构决策：is_decision=false，reason 说明原因，其余字段留空。
""",
    "file": """\
你是资深架构师，负责从一份设计/需求/代码文件中抽取架构决策记录（ADR）。

服务：{{service}}
仓库：{{repo}}
文件：{{source_ref}}

文件内容（可能被截断）：
{{content}}

""" + _COMMON_RULES + """
其它要求：
- 只抽取文件中明确表达或强烈暗示的决策，不要把待讨论的想法当作已定决策；
- title 说明“问题 + 选中的方案”；
- evidence.path 使用上面给出的文件路径。
""",
    "text": """\
你是资深架构师，负责把用户的一句话决策描述整理成架构决策记录（ADR）。

服务：{{service}}
仓库：{{repo}}
用户描述：
{{text}}

""" + _COMMON_RULES + """
其它要求：
- 用户描述可能很粗略，缺失信息用“待补充”占位，不要编造事实；
- title 说明“问题 + 选中的方案”；
- 如果用户描述的只是任务或缺陷而非架构决策：is_decision=false，reason 说明原因。
""",
}


def render_prompt(template: str, values: dict[str, Any]) -> str:
    """Substitute ``{{name}}`` placeholders; unknown names stay literal."""

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        value = values.get(name)
        return str(value) if value is not None else match.group(0)

    return _PLACEHOLDER_RE.sub(replace, template)


def default_prompt(source_kind: str) -> str:
    return DEFAULT_PROMPTS.get(source_kind) or DEFAULT_PROMPTS["text"]


def decision_schema_text() -> str:
    return json.dumps(DECISION_SCHEMA, ensure_ascii=False, indent=2)


def prompt_digest(template: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(template.encode("utf-8")).hexdigest()


def slugify(value: str, fallback: str = "decision") -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    slug = slug[:60].strip("-")
    return slug or fallback


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [line.strip("-* \t") for line in text.splitlines() if line.strip()]


def _as_text(value: Any, placeholder: str = "待补充") -> str:
    text = str(value or "").strip()
    return text or placeholder


def normalize_decision(value: Any) -> dict[str, Any]:
    """Coerce a model response into the decision shape the renderer needs."""
    data = value if isinstance(value, dict) else {}
    options = []
    for item in data.get("options") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            options.append(
                {
                    "name": name,
                    "pros": _as_list(item.get("pros")),
                    "cons": _as_list(item.get("cons")),
                }
            )
        elif str(item).strip():
            options.append({"name": str(item).strip(), "pros": [], "cons": []})

    consequences = data.get("consequences")
    if not isinstance(consequences, dict):
        consequences = {}
    evidence = []
    for item in data.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        line = item.get("line")
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        evidence.append({"path": path, "line": line, "note": str(item.get("note") or "").strip()})

    confidence = data.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None

    return {
        "is_decision": bool(data.get("is_decision")),
        "reason": str(data.get("reason") or "").strip(),
        "title": _as_text(data.get("title"), ""),
        "slug": str(data.get("slug") or "").strip(),
        "context": _as_text(data.get("context")),
        "drivers": _as_list(data.get("drivers")),
        "options": options,
        "chosen_option": str(data.get("chosen_option") or "").strip(),
        "outcome": _as_text(data.get("outcome")),
        "consequences": {
            "good": _as_list(consequences.get("good")),
            "bad": _as_list(consequences.get("bad")),
        },
        "confirmation": _as_text(data.get("confirmation")),
        "confidence": confidence,
        "evidence": evidence,
    }


def render_madr(
    decision: dict[str, Any],
    *,
    source_kind: str,
    source_ref: str,
    service: str,
    today: str | None = None,
) -> tuple[str, str, str]:
    """Render a MADR 4.0 document; return ``(title, slug, markdown)``.

    The status is always ``proposed``: only a human can accept a decision.
    """
    title = _as_text(decision.get("title"), "未命名架构决策").strip()
    title = re.sub(r"\s+", " ", title)[:MAX_TITLE_CHARS]
    slug = slugify(decision.get("slug") or title)
    date = today or _dt.date.today().isoformat()

    lines = ["---", f"status: {DEFAULT_STATUS}", f"date: {date}"]
    if source_kind == "commit":
        lines.append(f"source-commit: {source_ref}")
    lines += [f"source: {source_kind}:{source_ref}".rstrip(":"), "---", "", f"# {title}", ""]

    lines += ["## Context and Problem Statement", "", decision.get("context") or "待补充", ""]

    lines.append("## Decision Drivers")
    lines.append("")
    drivers = decision.get("drivers") or []
    lines += [f"* {item}" for item in drivers] or ["* 待补充"]
    lines.append("")

    lines.append("## Considered Options")
    lines.append("")
    options = decision.get("options") or []
    lines += [f"* {item['name']}" for item in options] or ["* 待补充"]
    lines.append("")

    lines.append("## Decision Outcome")
    lines.append("")
    chosen = decision.get("chosen_option") or ""
    outcome = decision.get("outcome") or ""
    if chosen:
        lines.append(f'Chosen option: "{chosen}", because {outcome or "待补充"}')
    else:
        lines.append(outcome or "待补充")
    lines.append("")

    lines.append("### Consequences")
    lines.append("")
    good = decision["consequences"]["good"]
    bad = decision["consequences"]["bad"]
    lines += [f"* Good, because {item}" for item in good]
    lines += [f"* Bad, because {item}" for item in bad]
    if not good and not bad:
        lines.append("* 待补充")
    lines.append("")

    lines.append("### Confirmation")
    lines.append("")
    lines.append(decision.get("confirmation") or "待补充")
    lines.append("")

    if options:
        lines.append("## Pros and Cons of the Options")
        lines.append("")
        for option in options:
            lines.append(f"### {option['name']}")
            lines.append("")
            rows = [f"* Good, because {item}" for item in option["pros"]]
            rows += [f"* Bad, because {item}" for item in option["cons"]]
            lines += rows or ["* 待补充"]
            lines.append("")

    evidence = decision.get("evidence") or []
    if evidence:
        lines.append("## More Information")
        lines.append("")
        for item in evidence:
            location = item["path"]
            if item.get("line"):
                location += f":{item['line']}"
            note = f" — {item['note']}" if item.get("note") else ""
            lines.append(f"* `{location}`{note}")
        lines.append("")

    if decision.get("confidence") is not None:
        lines.append(f"<!-- confidence: {decision['confidence']:.2f}, service: {service} -->")

    return title, slug, "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "DEFAULT_PROMPTS",
    "DEFAULT_STATUS",
    "HISTORY_KIND",
    "PROMPT_SOURCE_KINDS",
    "SOURCE_KINDS",
    "decision_schema_text",
    "default_prompt",
    "normalize_decision",
    "prompt_digest",
    "render_madr",
    "render_prompt",
    "slugify",
]
