"""Deterministic structured facts that feed domain-term candidate mining.

The extractor only reads the frozen CodeGraph repository and the snapshot
source provider.  Every fact keeps a file/line/symbol locator so terminal
candidates remain traceable to their source version.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TYPE_SUFFIXES = (
    ("event", ("Event", "Notification")),
    ("command", ("Command", "Cmd")),
    ("dto", ("Dto", "DTO", "Request", "Response", "Payload", "Param", "Params", "Vo", "VO")),
)
_MQ_PATTERNS = (
    "kafka", "rabbit", "publish", "produce", "subscribe", "consume",
    "listener", "topic", "queue", "exchange",
)
_BUSINESS_VERBS = {
    "approve", "reject", "cancel", "submit", "confirm", "complete", "close",
    "reopen", "pay", "refund", "ship", "deliver", "assign", "transfer",
    "record", "schedule", "notify", "invoice", "bill", "charge", "deposit",
    "withdraw", "grant", "revoke", "issue", "redeem", "reserve", "release",
    "reconcile", "settle", "publish", "archive", "restore", "activate",
    "deactivate", "suspend", "renew", "upgrade", "downgrade", "amend",
}
_TECHNICAL_METHOD_TOKENS = {
    "get", "set", "list", "find", "load", "save", "create", "update",
    "delete", "query", "map", "convert", "build", "parse", "to", "from",
    "on", "handle", "handler", "listener", "consumer", "producer", "test",
    "init", "main", "run", "start", "stop",
}
_TOPIC_RE = re.compile(
    r"""(?:topics?|queues?|exchanges?|routing[_-]?keys?|destination)\s*[=:]\s*(?:\[[^\]]*)?["']([^"']+)["']""",
    re.I,
)
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([\w.$-]+)[`\"\]]?\s*\((.*?)\)\s*;",
    re.I | re.S,
)
_SKIP_COLUMNS = {
    "id", "created", "createdat", "created_at", "updated", "updatedat",
    "updated_at", "deleted", "deletedat", "deleted_at", "version",
    "tenantid", "tenant_id",
}
_CONSTRAINT_PREFIXES = (
    "primary", "unique", "key", "index", "constraint", "foreign", "check",
    "partition", "fulltext", "spatial",
)


def _value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _classify_type(name: str) -> tuple[str, str]:
    for classification, suffixes in _TYPE_SUFFIXES:
        for suffix in suffixes:
            if name.endswith(suffix) and len(name) > len(suffix):
                return classification, name[: -len(suffix)]
    return "entity", name


def _name_tokens(value: str) -> list[str]:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value or "")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [item.lower() for item in re.findall(r"[A-Za-z\u0080-\uffff]+|\d+", value)]


def _snake_case(value: str) -> str:
    return "_".join(item.lower() for item in _name_tokens(value))


class TermFactExtractor:
    """Collect enums, typed classes, DB tables, message contracts and methods."""

    def __init__(self, reader, sources=None):
        self.reader = reader
        self.sources = sources

    def extract(self) -> dict[str, Any]:
        types = self._types()
        tables = self._tables(types)
        return {
            "enums": self._enums(),
            "types": types,
            "db_tables": tables,
            "messages": self._messages(),
            "service_methods": self._service_methods(),
            "languages": self._languages(),
        }

    # A missing helper or an older CodeGraph schema must not invalidate a
    # snapshot: structured facts are a best-effort projection.
    def _call(self, name: str, *args) -> list[Any]:
        function = getattr(self.reader, name, None)
        if function is None:
            return []
        try:
            return list(function(*args) or [])
        except Exception:
            return []

    def _enums(self) -> list[dict[str, Any]]:
        items = []
        for row in self._call("enum_nodes")[:200]:
            members = [
                str(member.get("name") or "")
                for member in self._call("enum_members", row.get("id"))
                if member.get("name")
            ]
            items.append({
                "node_id": row.get("id", ""),
                "name": row.get("name", ""),
                "qualified_name": row.get("qualified_name", ""),
                "file_path": row.get("file_path", ""),
                "start_line": int(row.get("start_line") or 0),
                "members": members,
            })
        return [item for item in items if item["name"]]

    def _types(self) -> list[dict[str, Any]]:
        items = []
        # shortcut: top 500 domain types by declaration order, upgrade to a
        # PageRank/PageRank-weighted cap when repositories grow past it.
        for row in self._call("domain_type_nodes")[:500]:
            name = str(row.get("name") or "")
            if not name or row.get("kind") == "enum":
                continue
            # Nested helper classes (Foo::Criterion) repeat per outer class and
            # are never domain terms.
            if str(row.get("qualified_name") or "").count("::") > 1:
                continue
            classification, base_name = _classify_type(name)
            items.append({
                "node_id": row.get("id", ""),
                "name": name,
                "base_name": base_name,
                "classification": classification,
                "kind": row.get("kind", ""),
                "qualified_name": row.get("qualified_name", ""),
                "file_path": row.get("file_path", ""),
                "start_line": int(row.get("start_line") or 0),
                "decorators": self._decorators(row.get("decorators")),
            })
        return items

    @staticmethod
    def _decorators(raw: Any) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return [str(raw)]
        return [str(item) for item in decoded] if isinstance(decoded, list) else []

    def _tables(self, types: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tables = self._sql_tables()
        known = {(item["name"].lower(), item["file_path"]) for item in tables}
        for item in types:
            annotation = " ".join(item.get("decorators") or []).lower()
            if not any(token in annotation for token in ("entity", "table", "document")):
                continue
            table_name = _snake_case(item["name"])
            match = re.search(r"""name\s*=\s*["']([^"']+)["']""", " ".join(item.get("decorators") or []))
            if match:
                table_name = match.group(1)
            key = (table_name.lower(), item["file_path"])
            if key in known:
                continue
            known.add(key)
            fields = self._call("domain_type_fields", item["node_id"])
            tables.append({
                "name": table_name,
                "file_path": item["file_path"],
                "start_line": item["start_line"],
                "columns": [str(field.get("name")) for field in fields if field.get("name")],
                "primary_key": [],
                "source": "orm",
            })
        return tables[:200]

    def _sql_tables(self) -> list[dict[str, Any]]:
        if not hasattr(self.sources, "list_files"):
            return []
        items = []
        try:
            entries = list(self.sources.list_files(globs=("*.sql",)) or [])
        except Exception:
            return []
        for entry in entries[:200]:
            path = entry.get("path") if isinstance(entry, dict) else getattr(entry, "path", "")
            text = self.sources.read_text(path) if path else None
            if not text:
                continue
            for match in _CREATE_TABLE_RE.finditer(text):
                columns: list[str] = []
                primary_key: list[str] = []
                for part in match.group(2).split(","):
                    token = part.strip().split()[0].strip('`"[]').lower() if part.strip() else ""
                    if not token or token in _CONSTRAINT_PREFIXES:
                        continue
                    if "primary key" in part.lower():
                        primary_key.append(token)
                    if token not in _SKIP_COLUMNS:
                        columns.append(token)
                items.append({
                    "name": match.group(1),
                    "file_path": path,
                    "start_line": text[: match.start()].count("\n") + 1,
                    "columns": columns,
                    "primary_key": primary_key,
                    "source": "sql",
                })
        return items

    def _messages(self) -> list[dict[str, Any]]:
        messages: dict[tuple[str, str], dict[str, Any]] = {}
        for pattern in _MQ_PATTERNS:
            for row in self._call("mq_producer_calls", pattern):
                symbol = str(row.get("caller") or row.get("name") or "")
                key = (symbol, "producer")
                if symbol and "test" not in _name_tokens(symbol) and key not in messages:
                    item = self._message_row(row, symbol, "producer", row.get("caller_node_id"))
                    if item:
                        messages[key] = item
            for row in self._call("mq_consumers", pattern):
                symbol = str(row.get("qualified_name") or row.get("name") or "")
                key = (symbol, "consumer")
                if symbol and "test" not in _name_tokens(symbol) and key not in messages:
                    item = self._message_row(row, symbol, "consumer", row.get("node_id"))
                    if item:
                        messages[key] = item
        return list(messages.values())[:100]

    def _message_row(self, row: dict[str, Any], symbol: str, role: str, node_id: Any) -> dict[str, Any] | None:
        path = str(row.get("file_path") or "")
        start_line = int(row.get("start_line") or 0)
        end_line = start_line
        locations = self._call("function_location", symbol)
        if locations:
            start_line = int(locations[0].get("start_line") or start_line)
            end_line = int(locations[0].get("end_line") or start_line)
        snippet = ""
        if path and hasattr(self.sources, "snippet"):
            try:
                snippet = self.sources.snippet(path, start_line, end_line) or ""
            except Exception:
                snippet = ""
        match = _TOPIC_RE.search(snippet)
        if not match and not set(_name_tokens(symbol.split("::")[-1])) & {
                "event", "message", "notification", "command"}:
            return None
        topic = match.group(1) if match else _topic_from_symbol(symbol)
        if not topic:
            return None
        return {
            "topic": topic,
            "symbol": symbol,
            "role": role,
            "node_id": node_id or "",
            "file_path": path,
            "start_line": start_line,
            "decorators": self._decorators(row.get("decorators")),
        }

    def _service_methods(self) -> list[dict[str, Any]]:
        methods = []
        seen: set[tuple[str, int]] = set()
        for row in self._call("functions"):
            name = str(_value(row, "name", ""))
            tokens = _name_tokens(name)
            if len(tokens) < 2 or tokens[0] not in _BUSINESS_VERBS or name.startswith("_"):
                continue
            if any(token in _TECHNICAL_METHOD_TOKENS for token in tokens[1:]):
                continue
            if bool(_value(row, "is_test", False)):
                continue
            visibility = str(_value(row, "visibility", "") or "")
            if visibility in {"private", "protected"}:
                continue
            key = (str(_value(row, "qualified_name", name)), int(_value(row, "start_line", 0) or 0))
            if key in seen:
                continue
            seen.add(key)
            methods.append({
                "name": name,
                "qualified_name": str(_value(row, "qualified_name", name)),
                "class_name": str(_value(row, "parent_class", "")),
                "file_path": str(_value(row, "file_path", "")),
                "start_line": int(_value(row, "start_line", 0) or 0),
                "language": str(_value(row, "language", "")),
            })
        return methods[:200]

    def _languages(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self._call("file_records"):
            language = str(row.get("language") or "")
            if language and language != "unknown":
                counts[language] = counts.get(language, 0) + 1
        return [
            {"language": language, "file_count": count}
            for language, count in sorted(counts.items(), key=lambda item: -item[1])
        ]


def _topic_from_symbol(symbol: str) -> str:
    """Fallback topic name when the source literal is unavailable."""
    name = symbol.split("::")[-1].split(".")[-1]
    tokens = _name_tokens(name)
    while tokens and tokens[0] in {"handle", "on", "consume", "listen", "process", "receive"}:
        tokens.pop(0)
    while tokens and tokens[-1] in {"handler", "listener", "consumer", "producer"}:
        tokens.pop()
    return "_".join(tokens) or name
