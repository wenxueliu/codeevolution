"""Versioned communication collector support matrix and budgets."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any


def load_tier1_rules() -> dict[str, Any]:
    raw = files("codeevolution.analysis.communication").joinpath("tier1_rules.json").read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != "communication-rules/v1":
        raise ValueError("unsupported communication rules")
    return value


def tier1_rules_digest() -> str:
    value = load_tier1_rules()
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


TIER1_RULES = load_tier1_rules()
TIER1_RULES_DIGEST = tier1_rules_digest()


__all__ = ["TIER1_RULES", "TIER1_RULES_DIGEST", "load_tier1_rules", "tier1_rules_digest"]
