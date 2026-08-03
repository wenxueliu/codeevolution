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
    def handler_for_route(self, file_path: str, route_line: int) -> dict[str, Any] | None: ...
    def api_call_chain(self, node_id: str, limit: int = 30) -> list[dict[str, Any]]: ...
    def type_schema(self, type_name: str) -> dict[str, Any] | None: ...


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
            handler = self._route_handler(route)
            endpoints.append(self._endpoint(method.upper(), path, route, handler))

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

    def _route_handler(self, route: dict[str, Any]) -> dict[str, Any] | None:
        if hasattr(self._source, "handler_for_route"):
            return self._source.handler_for_route(  # type: ignore[union-attr]
                route["file_path"], route["start_line"]
            )
        return None

    def _endpoint(
        self,
        method: str,
        path: str,
        route: dict[str, Any],
        handler: dict[str, Any] | None,
    ) -> ApiEndpoint:
        if not handler:
            return ApiEndpoint(
                method=method,
                path=path,
                handler_name="",
                file_path=route["file_path"],
                line=route["start_line"],
            )
        signature = handler.get("signature") or ""
        contract = self.parse_request_contract(signature)
        response_type = self.parse_return_type(signature)
        response_body = self._schema_for_type(response_type) if response_type else None
        call_chain = (
            self._source.api_call_chain(handler["id"])  # type: ignore[union-attr]
            if hasattr(self._source, "api_call_chain")
            else []
        )
        return ApiEndpoint(
            method=method,
            path=path,
            handler_name=handler["qualified_name"],
            file_path=handler["file_path"],
            line=handler["start_line"],
            params=self.parse_params(signature),
            return_type=response_type,
            request_headers=contract["headers"],
            query_params=contract["query"],
            path_params=contract["path"],
            request_body=contract["body"],
            response_body=response_body,
            call_chain=call_chain,
        )

    def _schema_for_type(self, type_name: str | None) -> dict | None:
        if not type_name:
            return None
        candidates = re.findall(r"[A-Za-z_$][\w$]*", type_name)
        ignored = {"void", "int", "long", "float", "double", "boolean", "string", "list", "map", "set", "optional", "commonresult", "commonpage"}
        model = next((item for item in reversed(candidates) if item.lower() not in ignored), None)
        schema = (
            self._source.type_schema(model)  # type: ignore[union-attr]
            if model and hasattr(self._source, "type_schema")
            else None
        )
        return {"type": type_name, "model": schema}

    def parse_request_contract(self, signature: str) -> dict[str, Any]:
        params_text = signature.split("(", 1)[1].rsplit(")", 1)[0] if "(" in signature else ""
        result: dict[str, Any] = {"headers": [], "query": [], "path": [], "body": None}
        for raw in self.split_params(params_text):
            annotation = re.search(r"@(RequestHeader|RequestBody|RequestParam|PathVariable)\s*(\([^)]*\))?", raw)
            cleaned = re.sub(r"@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s*", "", raw).strip()
            tokens = cleaned.split()
            if len(tokens) < 2:
                continue
            name, type_name = tokens[-1], " ".join(tokens[:-1])
            item = {"name": name, "type": type_name, "required": "required = false" not in raw}
            if annotation:
                explicit = re.search(r"(?:value|name)\s*=\s*['\"]([^'\"]+)|['\"]([^'\"]+)", annotation.group(2) or "")
                if explicit:
                    item["name"] = explicit.group(1) or explicit.group(2)
                kind = annotation.group(1)
                if kind == "RequestHeader":
                    result["headers"].append(item)
                elif kind == "RequestParam":
                    result["query"].append(item)
                elif kind == "PathVariable":
                    result["path"].append(item)
                elif kind == "RequestBody":
                    result["body"] = {**item, **(self._schema_for_type(type_name) or {})}
            else:
                result["query"].append(item)
        return result

    @staticmethod
    def split_params(value: str) -> list[str]:
        parts, start, depth = [], 0, 0
        for index, char in enumerate(value):
            depth += char in "<(["
            depth -= char in ">)]"
            if char == "," and depth == 0:
                parts.append(value[start:index].strip())
                start = index + 1
        tail = value[start:].strip()
        return parts + ([tail] if tail else [])

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
        for raw_param in ApiContractExtractor.split_params(params_text):
            param = re.sub(r"@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s*", "", raw_param).strip()
            if not param or param == "self":
                continue
            if ":" in param:
                params.append(param.split(":", 1)[0].strip())
            else:
                params.append(param.split()[-1])
        return params

    @staticmethod
    def parse_return_type(signature: str) -> str | None:
        if "->" in signature:
            return signature.split("->")[-1].strip().rstrip(":")
        if "(" in signature:
            value = signature.split("(", 1)[0].strip()
            return value or None
        return None
