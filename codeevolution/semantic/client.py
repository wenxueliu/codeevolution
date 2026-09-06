"""LLM transport port and OpenAI adapter."""

import json
from typing import Protocol

# Which (api_base, model) endpoints accept the `thinking` request param.
# None = unknown, True = supported, False = rejected (fall back to plain calls).
_THINKING_SUPPORT: dict[tuple[str | None, str], bool] = {}


class LLMClient(Protocol):
    def complete(
        self, prompt: str, max_tokens: int = 800, temperature: float = 0.2
    ) -> str | None: ...


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class OpenAILLMClient:
    def __init__(self, config: dict):
        self.config = config

    def complete(self, prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str | None:
        try:
            from openai import OpenAI
        except ImportError:
            # Not "no content": surface the real cause so callers don't
            # misreport it as a silent empty reply.
            return json.dumps(
                {"error": "openai 包未安装：请先执行 pip install 'openai>=1.0,<3'"}
            )
        try:
            # max_output_tokens（可选）：设置了就作为本次输出预算，覆盖各调用点的小默认值。
            # 未设置则用调用点自己的 max_tokens。
            configured_output = _int_or_none(self.config.get("max_output_tokens"))
            budget = configured_output if configured_output is not None else max_tokens

            # context_window（可选）：发送前粗略估算提示词 token 数，提前拦截可能溢出的请求。
            # 无 tokenizer，按"约 2 字符 ≈ 1 token"保守估算（中文 ~1 字符/token、英文 ~3-4 字符/token）。
            context_window = _int_or_none(self.config.get("context_window"))
            if context_window and (len(prompt) + 1) // 2 + budget > context_window:
                return json.dumps(
                    {
                        "error": (
                            f"提示词可能超出上下文窗口（估算输入约 {(len(prompt) + 1) // 2} token "
                            f"+ 输出 {budget} token > {context_window}）。"
                            "请在 LLM 设置中调大 context_window 或调小单次输出上限。"
                        )
                    }
                )

            client = OpenAI(
                api_key=self.config["api_key"],
                base_url=self.config["api_base"] or None,
            )
            kwargs = {
                "model": self.config["model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": budget,
            }
            if self.config.get("disable_thinking", True):
                endpoint = (self.config.get("api_base") or None, self.config["model"])
                if _THINKING_SUPPORT.get(endpoint) is not False:
                    try:
                        response = client.chat.completions.create(
                            **kwargs, extra_body={"thinking": {"type": "disabled"}}
                        )
                        _THINKING_SUPPORT[endpoint] = True
                        return response.choices[0].message.content
                    except Exception:
                        # Endpoint rejected the param (or failed with it); retry plain.
                        _THINKING_SUPPORT[endpoint] = False
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception as error:
            return json.dumps({"error": str(error)})
