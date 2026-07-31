"""Authorization model extraction."""

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

AUTH_PATTERNS = {
    "login_required": "authenticated",
    "permission_required": None,
    "has_permission": None,
    "has_role": None,
    "requires_auth": "authenticated",
    "require_auth": "authenticated",
    "authenticated": "authenticated",
    "preauthorize": None,
    "postauthorize": None,
    "secured": None,
    "rolesallowed": None,
    "permitall": "public",
    "denyall": "denied",
    "useguards": None,
    "roles": None,
    "requireauth": "authenticated",
    "public": "public",
    "auth_middleware": "authenticated",
    "authmiddleware": "authenticated",
    "requirepermission": None,
    "authorize": None,
}


class _Source(Protocol):
    def query(self, sql: str, params=None) -> list[dict[str, Any]]: ...


class AuthorizationExtractor:
    def __init__(self, source: _Source | Callable[[], list[dict]]):
        self.source = source

    def extract(self) -> list[dict]:
        if callable(self.source) and not hasattr(self.source, "query"):
            return self.source()
        results = []
        rows = self.source.query(  # type: ignore[union-attr]
            """SELECT id, name, qualified_name, file_path, start_line, decorators, kind
               FROM nodes WHERE kind IN ('function', 'method') AND decorators IS NOT NULL"""
        )
        for row in rows:
            try:
                decorators = (
                    json.loads(row["decorators"])
                    if isinstance(row["decorators"], str)
                    else row["decorators"]
                )
            except (json.JSONDecodeError, TypeError):
                continue
            roles, permissions, level = [], [], "unknown"
            for decorator in decorators:
                match = re.match(r"([\w.]+)(?:\(([^)]*)\))?", decorator.lstrip("@").lower())
                if not match:
                    continue
                name, arguments = match.group(1).split(".")[-1], match.group(2) or ""
                if name not in AUTH_PATTERNS:
                    continue
                level = AUTH_PATTERNS[name] or level
                for argument in arguments.split(",") if arguments else []:
                    value = argument.strip("[]()\"' ")
                    if value:
                        (
                            roles if name in {"has_role", "rolesallowed", "roles"} else permissions
                        ).append(value)
            if level != "unknown" or roles or permissions:
                results.append(self._entry(row, level, roles, permissions))
        middleware = self.source.query(  # type: ignore[union-attr]
            """SELECT id, name, qualified_name, file_path, start_line, kind FROM nodes
               WHERE kind IN ('function', 'method') AND (name LIKE '%auth%middleware%'
               OR name LIKE '%auth%guard%' OR name LIKE '%permission%check%'
               OR name LIKE '%authorize%' OR name LIKE '%authenticate%')"""
        )
        results.extend(self._entry(row, "middleware", [], []) for row in middleware)
        order = {"authenticated": 0, "middleware": 1, "public": 2}
        results.sort(key=lambda item: (order.get(item["auth_level"], 3), item["file"]))
        return results

    @staticmethod
    def _entry(row, level, roles, permissions):
        return {
            "function": row["qualified_name"],
            "file": row["file_path"],
            "line": row["start_line"],
            "auth_level": level,
            "roles": sorted(set(roles)),
            "permissions": sorted(set(permissions)),
        }
