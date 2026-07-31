"""API-contract extraction independent from the legacy knowledge facade."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

from ...codegraph_reader import HTTP_DECORATORS
from ...domain.knowledge import ApiContract, ApiEndpoint


class _QuerySource(Protocol):
    def route_nodes(self) -> list[dict[str, Any]]: ...
    def decorated_handlers(self) -> list[dict[str, Any]]: ...


class ApiContractExtractor:
    """Extract route and decorated-handler contracts from CodeGraph data.

    A zero-argument callback is still accepted for compatibility with the
    phase-one composable-step API. Production code passes a typed query source.
    """

    def __init__(self, source: _QuerySource | Callable[[], ApiContract]):
        self._source = source

    def extract(self) -> ApiContract:
        if callable(self._source) and not hasattr(self._source, "route_nodes"):
            return self._source()

        endpoints: list[ApiEndpoint] = []
        route_nodes = self._source.route_nodes()  # type: ignore[union-attr]
        for route in route_nodes:
            method, _, path = (route.get("name") or "").partition(" ")
            if not method or not path:
                continue
            endpoints.append(
                ApiEndpoint(
                    method=method.upper(),
                    path=path,
                    handler_name="",
                    file_path=route["file_path"],
                    line=route["start_line"],
                )
            )

        handlers = self._source.decorated_handlers()  # type: ignore[union-attr]
        for handler in handlers:
            decorators = self._decode_decorators(handler.get("decorators"))
            for decorator in decorators:
                decorator_name = decorator.lstrip("@").lower().split("(", 1)[0].split(".")[-1]
                if decorator_name not in HTTP_DECORATORS:
                    continue
                endpoints.append(
                    ApiEndpoint(
                        method=HTTP_DECORATORS[decorator_name] or "ANY",
                        path=self.infer_path(decorator, handler["name"], handler["file_path"]),
                        handler_name=handler["qualified_name"],
                        file_path=handler["file_path"],
                        line=handler["start_line"],
                        params=self.parse_params(handler.get("signature") or ""),
                        return_type=self.parse_return_type(handler.get("signature") or ""),
                        decorators=[decorator],
                    )
                )

        groups: dict[str, list[ApiEndpoint]] = defaultdict(list)
        for endpoint in endpoints:
            groups[self.resource_prefix(endpoint.path)].append(endpoint)
        return ApiContract(endpoints=endpoints, resource_groups=dict(sorted(groups.items())))

    @staticmethod
    def _decode_decorators(raw: Any) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str)]
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @staticmethod
    def infer_path(decorator: str, function_name: str, file_path: str = "") -> str:
        del file_path  # retained in the signature for compatibility and future framework rules
        match = re.search(r"['\"](/[^'\"]*)['\"]", decorator)
        if match:
            return match.group(1)
        name = function_name
        for prefix in ("get_", "post_", "put_", "delete_", "patch_", "head_"):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        parts = [":id" if part == "id" or part.endswith("_id") else part
                 for part in name.split("_") if part not in {"by", "with", "from", "for"}]
        return "/" + "/".join(parts) if parts else "/"

    @staticmethod
    def resource_prefix(path: str) -> str:
        parts = path.strip("/").split("/")
        for part in parts:
            if part.lower() not in {"api", "v1", "v2", "v3", "v4"} and not part.startswith(":"):
                return part
        return parts[-1] if parts else "root"

    @staticmethod
    def parse_params(signature: str) -> list[str]:
        if not signature or "(" not in signature:
            return []
        params_text = signature.split("(", 1)[1].rsplit(")", 1)[0]
        params = []
        for raw_param in params_text.split(","):
            param = raw_param.strip()
            if not param or param == "self":
                continue
            params.append(param.split(":", 1)[0].strip() if ":" in param else param.split()[0])
        return params

    @staticmethod
    def parse_return_type(signature: str) -> str | None:
        return signature.split("->")[-1].strip().rstrip(":") if "->" in signature else None
