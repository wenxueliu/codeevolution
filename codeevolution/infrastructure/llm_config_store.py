"""User-managed LLM configuration with private, atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..paths import data_dir
from ..platform import (
    atomic_replace,
    ensure_supported_storage_path,
    fsync_directory,
    fsync_file,
    set_private_permissions,
)


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
        result = {
            "api_key": api_key,
            "model": model,
            "api_base": str(data.get("api_base") or "").strip(),
        }
        if "disable_thinking" in data:
            result["disable_thinking"] = bool(data["disable_thinking"])
        if "disable_ssl_verification" in data:
            result["disable_ssl_verification"] = bool(data["disable_ssl_verification"])
        if "context_window" in data:
            result["context_window"] = _optional_int(data["context_window"])
        if "max_output_tokens" in data:
            result["max_output_tokens"] = _optional_int(data["max_output_tokens"])
        return result

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
        }
        for key in (
            "disable_thinking",
            "disable_ssl_verification",
            "context_window",
            "max_output_tokens",
        ):
            if key in config:
                payload[key] = (
                    bool(config[key])
                    if key in {"disable_thinking", "disable_ssl_verification"}
                    else _optional_int(config[key])
                )
        ensure_supported_storage_path(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                fsync_file(handle)
            set_private_permissions(temporary, sensitive=True)
            atomic_replace(temporary, self.path)
            fsync_directory(self.path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return payload

    def delete(self) -> bool:
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed
