"""LLM-powered knowledge extraction via litellm.

Phase 3 — requires LLM for semantic understanding:

  1. Business naming       — what does this function DO in business terms?
  2. Business rules        — extract validation, conditions, transformations
  3. Error scenarios       — catalog error types and triggering conditions
  4. State machine         — detect state enums and their transition graph
  5. Architecture decisions — extract design rationale from comments/docs

All models are accessed through litellm (OpenAI, Anthropic, etc.).
Set OPENAI_API_KEY or ANTHROPIC_API_KEY in the environment.
Model can be overridden via CODEHISTORY_LLM_MODEL.
"""

from concurrent.futures import ThreadPoolExecutor

from .semantic.client import LiteLLMClient
from .semantic.config import get_llm_config
from .semantic.json_parser import parse_json
from .semantic.models import BusinessDescription, BusinessRule, ErrorScenario, StateMachineDef

# ── Configuration ──────────────────────────────────────────────────────


def _get_llm_config() -> dict | None:
    """Check if any LLM provider is configured."""
    return get_llm_config()


def is_available() -> bool:
    return _get_llm_config() is not None


# ── LLM client ─────────────────────────────────────────────────────────


def _call_llm(
    prompt: str,
    max_tokens: int = 800,
    temperature: float = 0.2,
) -> str | None:
    """Call LLM via litellm. Returns response text or None."""
    config = _get_llm_config()
    if not config:
        return None

    return LiteLLMClient(config).complete(prompt, max_tokens, temperature)


def _parse_json(content: str | None) -> dict | None:
    """Robust JSON extraction from LLM response."""
    return parse_json(content)


# ── 1. Business naming ─────────────────────────────────────────────────


def explain_business_purpose(
    func_name: str,
    signature: str | None,
    docstring: str | None,
    decorators: list[str] | None,
    file_path: str,
    source_snippet: str | None = None,
    callee_names: list[str] | None = None,
    caller_names: list[str] | None = None,
) -> BusinessDescription | None:
    """Generate a business-level description of what a function does.

    Uses function metadata + surrounding context to produce a description
    suitable for product managers and architects.
    """
    config = _get_llm_config()
    if not config:
        return None

    # Build context
    parts = [f"Function: {func_name}"]
    if signature:
        parts.append(f"Signature: {signature}")
    if docstring:
        parts.append(f"Docstring: {docstring}")
    if decorators:
        parts.append(f"Decorators: {', '.join(decorators)}")
    parts.append(f"File: {file_path}")
    if source_snippet:
        # Truncate to ~2000 chars for token budget
        parts.append(f"Source:\n```\n{source_snippet[:2000]}\n```")
    if callee_names:
        parts.append(f"Calls (downstream): {', '.join(callee_names[:15])}")
    if caller_names:
        parts.append(f"Called by (upstream): {', '.join(caller_names[:15])}")

    prompt = f"""You analyze code to extract business knowledge for product managers.

Given the following function metadata, explain what business purpose this function serves.

Context:
{chr(10).join(parts)}

Output JSON:
{{
  "summary_en": "1-2 sentence English summary of what this function does in business terms",
  "summary_zh": "1-2 sentence Chinese summary",
  "business_domain": "Which business domain does this belong to? (e.g. 'User Management', 'Payment', 'Order Processing', 'Inventory', 'Reporting')",
  "role": "What role does this function play? One of: 'Entry Point' (receives external requests), 'Business Logic' (orchestrates domain operations), 'Data Access' (reads/writes data), 'Integration' (calls external services), 'Middleware' (cross-cutting concern), 'Utility' (helper function)",
  "key_responsibilities": ["short phrase describing one key responsibility", ...]
}}

JSON:"""

    response = _call_llm(prompt, max_tokens=500, temperature=0.2)
    data = _parse_json(response)
    if not data or "raw" in data:
        return None

    return BusinessDescription(
        function_name=func_name,
        summary_en=data.get("summary_en", ""),
        summary_zh=data.get("summary_zh", ""),
        business_domain=data.get("business_domain", ""),
        role=data.get("role", ""),
        key_responsibilities=data.get("key_responsibilities", []),
    )


# ── 2. Business rules ──────────────────────────────────────────────────


def extract_business_rules(
    func_name: str,
    source_snippet: str,
    file_path: str = "",
) -> list[BusinessRule]:
    """Extract business rules from a function's source code.

    Identifies validation gates, state-dependent logic, and business
    invariants embedded in if-else branches and assertions.
    """
    config = _get_llm_config()
    if not config:
        return []

    prompt = f"""You analyze code to extract business rules. A business rule is an explicit check or condition that enforces a business constraint.

Analyze this function and extract ALL business rules:

Function: {func_name}
File: {file_path}

Source code:
```
{source_snippet[:3000]}
```

For each business rule found, output JSON:
{{
  "rules": [
    {{
      "rule_type": "validation|transformation|authorization|workflow|calculation",
      "description_en": "What this rule enforces in plain English",
      "description_zh": "Chinese description",
      "condition": "The if-condition, guard clause, or check being performed",
      "failure_mode": "What happens when the rule is violated (throw exception, return error, redirect, silently default)"
    }}
  ]
}}

Focus on BUSINESS rules, not technical details. Skip null checks, type checks, and framework boilerplate. Look for:
- Validation of business data (e.g., "order total must be > 0")
- Authorization checks (e.g., "only admins can cancel orders")
- Business state transitions (e.g., "shipped orders cannot be cancelled")
- Calculation rules (e.g., "discount is capped at 20%")
- Workflow rules (e.g., "approval required above $1000")

If no business rules are found, return {{"rules": []}}.

JSON:"""

    response = _call_llm(prompt, max_tokens=600, temperature=0.1)
    data = _parse_json(response)
    if not data:
        return []

    rules_data = data.get("rules", [])
    if isinstance(rules_data, str):  # LLM sometimes returns rules as a string
        return []

    return [
        BusinessRule(
            function_name=func_name,
            rule_type=r.get("rule_type", "validation"),
            description_en=r.get("description_en", ""),
            description_zh=r.get("description_zh", ""),
            condition=r.get("condition", ""),
            failure_mode=r.get("failure_mode", ""),
        )
        for r in rules_data
    ]


# ── 3. Error scenarios ─────────────────────────────────────────────────


def extract_error_scenarios(
    func_name: str,
    source_snippet: str,
    file_path: str = "",
) -> list[ErrorScenario]:
    """Extract error handling patterns from source code.

    Identifies what errors can occur, under what conditions, and how
    they are handled.
    """
    config = _get_llm_config()
    if not config:
        return []

    prompt = f"""You analyze code to catalog error scenarios for operations and testing teams.

Analyze this function and extract ALL error scenarios:

Function: {func_name}
File: {file_path}

Source code:
```
{source_snippet[:3000]}
```

For each error scenario found, output JSON:
{{
  "scenarios": [
    {{
      "error_type": "Name of the error/exception (e.g., 'ValidationError', 'OrderNotFound', 'PaymentFailed')",
      "trigger_condition": "What conditions trigger this error?",
      "handling": "throw|return error|log and continue|retry|delegate to caller",
      "user_facing": true/false — is this error message visible to end users?
    }}
  ]
}}

Include ALL error paths: throw/raise statements, error return values, HTTP error responses, try-catch blocks.

JSON:"""

    response = _call_llm(prompt, max_tokens=600, temperature=0.1)
    data = _parse_json(response)
    if not data:
        return []

    scenarios = data.get("scenarios", [])
    if isinstance(scenarios, str):
        return []

    return [
        ErrorScenario(
            function_name=func_name,
            error_type=s.get("error_type", "UnknownError"),
            trigger_condition=s.get("trigger_condition", ""),
            handling=s.get("handling", "throw"),
            user_facing=s.get("user_facing", False),
        )
        for s in scenarios
    ]


# ── 4. State machine ───────────────────────────────────────────────────


def detect_state_machine(
    entity_name: str,
    enum_name: str,
    enum_members: list[str],
    transition_functions: list[dict],
) -> StateMachineDef | None:
    """Detect a state machine from an enum and its usage context.

    Args:
        entity_name: The business entity (e.g., "Order")
        enum_name: The enum type name (e.g., "OrderStatus")
        enum_members: List of enum values (e.g., ["PENDING", "PAID", "SHIPPED"])
        transition_functions: Functions that reference this enum, with snippets.
            [{name, snippet, relevant_lines}]
    """
    config = _get_llm_config()
    if not config:
        return None

    func_context = "\n".join(
        f"- {f['name']}: {f.get('relevant_lines', '')[:200]}" for f in transition_functions[:8]
    )

    prompt = f"""You analyze code to extract state machines for product and testing teams.

Entity: {entity_name}
Status enum: {enum_name} = [{", ".join(enum_members)}]

Functions that use this enum:
{func_context}

Analyze the code patterns and identify the state machine:
1. What are the valid states?
2. What are the valid transitions between states?
3. What triggers each transition (e.g., "user clicks 'Pay'", "admin approves")?
4. What guard conditions exist (e.g., "only if payment is confirmed")?
5. What is the initial state? What are terminal states?

Output JSON:
{{
  "entity": "{entity_name}",
  "states": ["state1", "state2", ...],
  "initial_state": "initial state name",
  "terminal_states": ["terminal state names"],
  "transitions": [
    {{
      "from": "source state",
      "to": "target state",
      "trigger": "what causes this transition (in business terms)",
      "guard_condition": "any guard condition or empty string"
    }}
  ]
}}

If you cannot reliably determine the state machine from the provided context, return:
{{"entity": "{entity_name}", "states": [], "initial_state": "", "terminal_states": [], "transitions": []}}

JSON:"""

    response = _call_llm(prompt, max_tokens=600, temperature=0.2)
    data = _parse_json(response)
    if not data or not data.get("states"):
        return None

    return StateMachineDef(
        entity=data.get("entity", entity_name),
        states=data.get("states", []),
        initial_state=data.get("initial_state", ""),
        terminal_states=data.get("terminal_states", []),
        transitions=data.get("transitions", []),
    )


# ── 5. Architecture decisions ──────────────────────────────────────────


def extract_architecture_notes(
    func_name: str,
    source_snippet: str,
    docstring: str | None,
    file_path: str = "",
) -> dict | None:
    """Extract architecture-level notes from comments and code patterns.

    Identifies design patterns, architectural decisions, and rationale
    embedded in docstrings and inline comments.
    """
    config = _get_llm_config()
    if not config:
        return None

    # Prioritize docstrings + comments; only include first 100 lines of code
    code_lines = source_snippet.split("\n")
    comments_and_docs = [
        l for l in code_lines if l.strip().startswith(("//", "#", "/*", "*", '"""', "'''"))
    ]
    # Also include first meaningful code lines for context
    non_comment_lines = [l for l in code_lines if not l.strip().startswith(("//", "#", "/*", "*"))][
        :30
    ]

    context = "\n".join(comments_and_docs[:30] + ["---"] + non_comment_lines)

    prompt = f"""You analyze code to extract architectural decisions for architects and developers.

Function: {func_name}
File: {file_path}
{"Docstring: " + docstring if docstring else ""}

Comments, docstrings, and surrounding code:
```
{context[:2500]}
```

Identify:
1. Design patterns used (e.g., "Factory pattern", "Strategy pattern", "Observer")
2. Architecture decisions visible in the code (e.g., "uses async I/O for non-blocking", "in-memory cache with TTL", "event-driven architecture")
3. Trade-offs or technical debt markers (e.g., "TODO: replace with...", "HACK:", "FIXME:", performance notes)

Output JSON:
{{
  "design_patterns": ["pattern name", ...],
  "architecture_notes": "1-2 paragraph summary of notable architecture decisions",
  "trade_offs": ["notable trade-off or tech-debt marker", ...]
}}

If nothing notable is found, return empty arrays and "No notable architecture decisions found.".

JSON:"""

    response = _call_llm(prompt, max_tokens=500, temperature=0.2)
    return _parse_json(response)


# ── Batch processing ────────────────────────────────────────────────────


def batch_explain_functions(
    functions: list[dict],
    max_concurrency: int = 3,
    progress_callback=None,
) -> list[dict]:
    """Explain business purpose for a batch of functions.

    Args:
        functions: [{name, signature, docstring, decorators, file_path,
                     source_snippet?, callee_names?, caller_names?}, ...]
        max_concurrency: Max parallel LLM calls (default 3 to respect rate limits)
        progress_callback: Optional callback(current, total)

    Returns:
        [{function_name, summary_en, summary_zh, business_domain, role, error?}, ...]
    """
    if not is_available():
        return [{"function_name": f["name"], "error": "LLM not configured"} for f in functions]

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")

    def explain_one(func: dict) -> dict:
        try:
            desc = explain_business_purpose(
                func_name=func["name"],
                signature=func.get("signature"),
                docstring=func.get("docstring"),
                decorators=func.get("decorators"),
                file_path=func.get("file_path", ""),
                source_snippet=func.get("source_snippet"),
                callee_names=func.get("callee_names"),
                caller_names=func.get("caller_names"),
            )
            if desc:
                return {
                    "function_name": desc.function_name,
                    "summary_en": desc.summary_en,
                    "summary_zh": desc.summary_zh,
                    "business_domain": desc.business_domain,
                    "role": desc.role,
                    "key_responsibilities": desc.key_responsibilities,
                }
            return {
                "function_name": func["name"],
                "error": "LLM response parsing failed",
            }
        except Exception as error:
            return {"function_name": func["name"], "error": str(error)}

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        for i, result in enumerate(executor.map(explain_one, functions)):
            results.append(result)
            if progress_callback:
                progress_callback(i + 1, len(functions))

    return results


# ── Legacy compatibility ───────────────────────────────────────────────


def explain_feature(
    feature_name: str,
    description: str,
    description_zh: str,
    call_chain: list[dict],
    features_context: list[dict] | None = None,
) -> dict | None:
    """Legacy wrapper: explain a feature through its call chain."""
    config = _get_llm_config()
    if not config:
        return None

    if not call_chain:
        return {
            "en": f"{feature_name} has no internal call chain.",
            "zh": f"{feature_name} 无内部调用链。",
        }

    chain_lines = []
    for edge in call_chain:
        indent = "  " * edge.get("depth", 0)
        chain_lines.append(f"{indent}{edge.get('from', '?')} → {edge.get('to', '?')}()")
    chain_text = "\n".join(chain_lines)

    callee_context = ""
    if features_context:
        callee_lines = [
            f"- {f.get('canonical_name', '')}: {f.get('description', '')}"
            for f in features_context[:10]
        ]
        callee_context = "\n".join(callee_lines)

    related_functions = f"Related functions:\n{callee_context}" if callee_context else ""
    prompt = f"""You are analyzing code evolution. Explain the implementation of this feature.

Feature: {feature_name}
Description: {description}

Call chain:
{chain_text}

{related_functions}

Explain in 3-5 sentences: (1) high-level purpose, (2) step-by-step flow through the
call chain, and (3) notable patterns.

Output JSON: {{"en": "English explanation", "zh": "Chinese explanation"}}

JSON:"""

    try:
        response = _call_llm(prompt, max_tokens=500, temperature=0.3)
        data = _parse_json(response)
        if data and "en" in data:
            return data
    except Exception as e:
        return {"en": f"LLM error: {e}", "zh": f"LLM 错误: {e}"}

    return None
