"""Public interface (non-HTTP) contract: the externally callable surface.

An SDK/library repository has no HTTP routes, MQ channels or cron entries, so
the API-contract extractor finds nothing and the topology reachability roots
end up empty.  The real contract of such a repo is its public surface.

How an entry is decided
-----------------------
1. **Declared exports win.**  A Python package entry module (a top-level
   ``<pkg>/__init__.py`` or ``src/<pkg>/__init__.py``) that re-exports names
   with ``from .module import Name`` states the public surface explicitly.
   Those names are the contract even if the package also uses them internally.
2. **Otherwise the surface is the call-graph root set.**  A public function or
   method is an entry only when no other non-test code in this repository calls
   it, its owning class is not instantiated by this repository's non-test code,
   and it is not a nested/local function.  Anything called from inside the
   repo is implementation, not contract.

Rule 2 is the "not called by another interface of the same service" rule: an
SDK's real external entry points are the ones the service itself does not
already consume.

3. **HTTP is excluded.**  Route/decorated handlers and ``http.server``
   dispatch methods belong to the API contract; they are removed from both the
   declared and the inferred surface so the two contracts never repeat the same
   symbol.

See ``design/public-interface-contract.md`` for the capability, its business
description and the full rule set.
"""

from __future__ import annotations

import re
from typing import Any

INTERFACE_VERSION = 2

# Path segments that never carry importable surface code.
EXCLUDED_SEGMENTS = frozenset({
    "tests", "test", "internal", "private", "_private", "examples",
    "example", "fixtures", "docs", "benchmark", "benchmarks",
    "samples", "build", "dist", "node_modules", "scripts",
})

# Languages where CodeGraph's is_exported flag is trustworthy; elsewhere
# (notably Python, where 0.9.x leaves it 0 for every node) the surface is
# decided positionally/by convention.
EXPORT_FLAG_LANGUAGES = frozenset({"typescript", "javascript", "go"})

_TEST_PATH_MARKERS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
    "/e2e/", "/integration/", "/fixtures/", "/__mocks__/",
)
_TEST_FILE_MARKERS = (".test.", ".spec.", "_test.", "test_", "conftest.py")

_KIND_PRIORITY = {"class": 0, "function": 1, "constant": 2, "variable": 3}

_RELATIVE_IMPORT = re.compile(r"^from\s+(\.+[\w.]*)\s+import\s+(.+)$", re.S)


def _is_test_path(file_path: str) -> bool:
    path = "/" + file_path.replace("\\", "/").lower().lstrip("/")
    if any(marker in path for marker in _TEST_PATH_MARKERS):
        return True
    name = path.rsplit("/", 1)[-1]
    return any(marker in name for marker in _TEST_FILE_MARKERS)


def _is_public_path(file_path: str) -> bool:
    segments = {part.lower() for part in file_path.replace("\\", "/").split("/")}
    return not segments & EXCLUDED_SEGMENTS


def _is_package_entry(file_path: str) -> bool:
    """True for ``pkg/__init__.py`` and ``src/pkg/__init__.py`` only."""
    parts = [part for part in file_path.replace("\\", "/").split("/") if part]
    if not parts or parts[-1] != "__init__.py":
        return False
    if len(parts) == 2:
        return True
    return len(parts) == 3 and parts[0] == "src"


def _module_name(file_path: str) -> str:
    path = file_path.replace("\\", "/")
    if path.endswith("/__init__.py"):
        path = path[: -len("/__init__.py")]
    elif path == "__init__.py":
        path = ""
    elif path.endswith(".py"):
        path = path[:-3]
    parts = [part for part in path.split("/") if part and part != "src"]
    return ".".join(parts)


def _parse_relative_imports(signature: str | None) -> list[tuple[str, str, str]]:
    """Return ``(module, exported_name, source_name)`` for a relative import.

    Only relatives imports are considered: ``from .contracts import A, B as C``
    declares the package's own surface; absolute imports are somebody else's.
    """
    text = (signature or "").strip()
    match = _RELATIVE_IMPORT.match(text)
    if not match:
        return []
    module, names = match.group(1), match.group(2)
    names = names.strip().strip("()")
    result: list[tuple[str, str, str]] = []
    for raw in names.split(","):
        token = " ".join(raw.split())
        if not token or token == "*":
            continue
        if " as " in token:
            source_name, exported_name = (part.strip() for part in token.split(" as ", 1))
        else:
            source_name = exported_name = token
        if source_name and not exported_name.startswith("_"):
            result.append((module, exported_name, source_name))
    return result


def _module_candidates(entry_path: str, module: str) -> list[str]:
    package_dir = entry_path.rsplit("/", 1)[0] if "/" in entry_path else ""
    relative = module.lstrip(".")
    if not relative:
        return [entry_path]
    base = relative.replace(".", "/")
    candidates = [f"{base}.py", f"{base}/__init__.py"]
    return [f"{package_dir}/{item}" if package_dir else item for item in candidates]


class InterfaceContractExtractor:
    def __init__(self, source: Any):
        # Callable fallback keeps the composable-step protocol used by the
        # other knowledge extractors.
        self.source = source
        self._callers: dict[str, list[dict[str, Any]]] = {}
        self._instantiators: dict[str, list[dict[str, Any]]] = {}
        self._symbols: dict[str, list[dict[str, Any]]] = {}
        self._http_handlers: set[str] = set()

    def extract(self) -> list[dict[str, Any]]:
        if callable(self.source) and not hasattr(self.source, "functions"):
            return self.source()
        self._callers = self._caller_index()
        self._instantiators = self._instantiator_index()
        getter = getattr(self.source, "http_handler_node_ids", None)
        self._http_handlers = set(getter() or []) if callable(getter) else set()
        declared = self._declared_entries()
        if declared:
            return declared
        return self._inferred_entries()

    # ---- shared evidence -------------------------------------------------

    def _caller_index(self) -> dict[str, list[dict[str, Any]]]:
        getter = getattr(self.source, "caller_edges", None)
        if not callable(getter):
            return {}
        index: dict[str, list[dict[str, Any]]] = {}
        for row in getter() or []:
            index.setdefault(row["callee_id"], []).append(row)
        return index

    def _instantiator_index(self) -> dict[str, list[dict[str, Any]]]:
        getter = getattr(self.source, "instantiation_edges", None)
        if not callable(getter):
            return {}
        index: dict[str, list[dict[str, Any]]] = {}
        for row in getter() or []:
            index.setdefault(row["class_qualified_name"], []).append(row)
        return index

    def _internal_callers(self, node_id: str) -> list[dict[str, Any]]:
        """Non-test, non-recursive call sites inside this repository."""
        result = []
        for row in self._callers.get(node_id) or []:
            if row.get("caller_id") == node_id:
                continue
            if _is_test_path(row.get("caller_file") or ""):
                continue
            result.append({
                "name": row.get("caller_name") or "",
                "qualified_name": row.get("caller_qualified_name") or "",
                "file_path": row.get("caller_file") or "",
                "line": row.get("caller_line") or 0,
                "call_line": row.get("call_line") or 0,
            })
        return result

    def _internal_instantiations(self, class_qualified_name: str) -> list[dict[str, Any]]:
        result = []
        for row in self._instantiators.get(class_qualified_name) or []:
            if _is_test_path(row.get("caller_file") or ""):
                continue
            result.append({
                "name": row.get("caller_name") or "",
                "qualified_name": row.get("caller_qualified_name") or "",
                "file_path": row.get("caller_file") or "",
                "line": row.get("caller_line") or 0,
            })
        return result

    def _symbols_in(self, file_path: str) -> list[dict[str, Any]]:
        if file_path in self._symbols:
            return self._symbols[file_path]
        getter = getattr(self.source, "symbols_in_file", None)
        rows = getter(file_path) if callable(getter) else []
        self._symbols[file_path] = list(rows or [])
        return self._symbols[file_path]

    # ---- declared exports ------------------------------------------------

    def _declared_entries(self) -> list[dict[str, Any]]:
        getter = getattr(self.source, "package_entry_imports", None)
        if not callable(getter):
            return []
        entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in getter() or []:
            entry_path = row.get("file_path") or ""
            if not _is_package_entry(entry_path):
                continue
            for module, exported, source_name in _parse_relative_imports(row.get("signature")):
                key = (entry_path, exported)
                if key in seen:
                    continue
                node = self._resolve_symbol(entry_path, module, source_name)
                if node is None:
                    continue
                if self._is_http_handler(node.get("id") or node.get("node_id")):
                    # Already documented by the API contract.
                    continue
                seen.add(key)
                entry = self._entry(node, "declared", entry_path)
                if exported != entry["name"]:
                    # ``from .core import create_client as build`` exports the
                    # alias, not the defining name.
                    entry["declared_name"] = entry["name"]
                    entry["name"] = exported
                entries.append(entry)
        entries.sort(key=lambda item: (item["file_path"], item["line"], item["name"]))
        return entries

    def _resolve_symbol(self, entry_path: str, module: str, name: str) -> dict[str, Any] | None:
        for file_path in _module_candidates(entry_path, module):
            matches = [
                row for row in self._symbols_in(file_path)
                if row.get("name") == name and row.get("kind") in _KIND_PRIORITY
            ]
            if matches:
                matches.sort(key=lambda row: _KIND_PRIORITY.get(row.get("kind"), 9))
                return matches[0]
        return None

    # ---- inferred roots --------------------------------------------------

    def _inferred_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for row in self.source.functions():
            if not self._keep(row):
                continue
            if self._is_nested(row):
                continue
            node_id = self._get(row, "node_id") or ""
            if self._is_http_handler(node_id):
                continue
            if self._internal_callers(node_id):
                continue
            if self._get(row, "kind") == "method":
                owner = (self._get(row, "qualified_name") or "").rsplit("::", 1)[0]
                if self._internal_instantiations(owner):
                    continue
            entries.append(self._entry(row, "root", None))
        return entries

    def _is_http_handler(self, node_id: str | None) -> bool:
        return bool(node_id) and node_id in self._http_handlers

    def _is_nested(self, row: Any) -> bool:
        qualified = self._get(row, "qualified_name") or ""
        if "::" not in qualified:
            return False
        parent = qualified.rsplit("::", 1)[0]
        getter = getattr(self.source, "node_kind", None)
        parent_kind = getter(parent) if callable(getter) else None
        if parent_kind is None:
            # A class method's parent is the class; anything deeper is nested.
            return qualified.count("::") > 1
        return parent_kind in {"function", "method"}

    # ---- entry shape -----------------------------------------------------

    def _entry(self, row: Any, surface: str, declared_by: str | None) -> dict[str, Any]:
        node_id = self._get(row, "node_id") or self._get(row, "id") or ""
        file_path = self._get(row, "file_path") or ""
        kind = self._get(row, "kind") or ""
        qualified = self._get(row, "qualified_name") or ""
        callers = self._internal_callers(node_id) if node_id else []
        entry = {
            "name": self._get(row, "name") or "",
            "qualified_name": qualified,
            "kind": kind,
            "signature": self._get(row, "signature") or "",
            "language": self._get(row, "language") or "",
            "file_path": file_path,
            "line": self._get(row, "start_line") or 0,
            "module": _module_name(file_path),
            "node_id": node_id,
            "docstring": self._get(row, "docstring") or "",
            "public_surface": surface,
            "declared_by": declared_by or "",
            "caller_count": len(callers),
            "internal_callers": callers[:5],
        }
        if kind == "class":
            entry["member_count"] = sum(
                1 for item in self._symbols_in(file_path)
                if item.get("kind") == "method"
                and (item.get("qualified_name") or "").startswith(qualified + "::")
                and not (item.get("name") or "").startswith("_")
            )
        return entry

    @staticmethod
    def _get(row: Any, key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    @staticmethod
    def _keep(row: Any) -> bool:
        get = InterfaceContractExtractor._get
        name = get(row, "name") or ""
        if not name or name.startswith("_") or get(row, "is_test"):
            return False
        if (get(row, "visibility") or "").lower() in {"private", "protected", "internal", "module"}:
            return False
        if (get(row, "language") or "").lower() == "vue":
            # The Vue surface is the component itself; setup() internals are
            # noise here (page extraction owns that layer).
            return False
        if not _is_public_path(get(row, "file_path") or ""):
            return False
        if _is_test_path(get(row, "file_path") or ""):
            return False
        if (get(row, "language") or "").lower() in EXPORT_FLAG_LANGUAGES:
            return bool(get(row, "is_exported"))
        return True
