"""User-managed LLM configuration with private, atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class LLMConfigStore:
    def __init__(self, path: str | Path | None = None):
        data_dir = Path(
            os.environ.get("CODEHISTORY_DATA_DIR", str(Path.home() / ".codehistory"))
        )
        self.path = Path(path) if path else data_dir / "llm-config.json"

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
