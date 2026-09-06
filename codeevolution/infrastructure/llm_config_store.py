"""User-managed LLM configuration with private, atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..paths import data_dir


def _optional_int(value) -> int | None:
    """Coerce a config value to a positive int, or None when blank/invalid."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class LLMConfigStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "llm-config.json"

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        api_key = str(data.get("api_key") or "").strip()
        model = str(data.get("model") or "").strip()
        if not api_key or not model:
            return None
        return {
            "api_key": api_key,
            "model": model,
            "api_base": str(data.get("api_base") or "").strip(),
            # 推理模型会把 max_tokens 预算几乎全部花在 reasoning 上，
            # 导致 content 为空。默认关闭 thinking，保证结构化抽取能产出结果。
            "disable_thinking": bool(data.get("disable_thinking", True)),
            # 可选：模型上下文窗口 / 单次最大输出（token）。留空 = 不约束。
            "context_window": _optional_int(data.get("context_window")),
            "max_output_tokens": _optional_int(data.get("max_output_tokens")),
        }

    def save(self, config: dict) -> dict:
        api_key = str(config.get("api_key") or "").strip()
        model = str(config.get("model") or "").strip()
        if not api_key:
            raise ValueError("API Key 不能为空")
        if not model:
            raise ValueError("模型名称不能为空")
        payload = {
            "api_key": api_key,
            "model": model,
            "api_base": str(config.get("api_base") or "").strip(),
            "disable_thinking": bool(config.get("disable_thinking", True)),
            "context_window": _optional_int(config.get("context_window")),
            "max_output_tokens": _optional_int(config.get("max_output_tokens")),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return payload

    def delete(self) -> bool:
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed
