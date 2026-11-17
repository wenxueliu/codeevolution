"""Public interface (non-HTTP) contract: the externally callable surface.

An SDK/library repository has no HTTP routes, MQ channels or cron entries, so
the API-contract extractor finds nothing and the topology reachability roots
end up empty.  The real contract of such a repo is its public surface: the
top-level functions and public class methods an external caller can invoke.
"""

from typing import Any

INTERFACE_VERSION = 1

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


def _is_public_path(file_path: str) -> bool:
    segments = {part.lower() for part in file_path.replace("\\", "/").split("/")}
    return not segments & EXCLUDED_SEGMENTS


class InterfaceContractExtractor:
    def __init__(self, source: Any):
        # Callable fallback keeps the composable-step protocol used by the
        # other knowledge extractors.
        self.source = source

    def extract(self) -> list[dict[str, Any]]:
        if callable(self.source) and not hasattr(self.source, "functions"):
            return self.source()
        return [entry for row in self.source.functions() if self._keep(row)
                for entry in [self._entry(row)]]

    def _entry(self, row: Any) -> dict[str, Any]:
        get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
        return {
            "name": get("name") or "",
            "qualified_name": get("qualified_name") or "",
            "kind": get("kind") or "",
            "signature": get("signature") or "",
            "language": get("language") or "",
            "file_path": get("file_path") or "",
            "line": get("start_line") or 0,
            "node_id": get("node_id") or "",
        }

    @staticmethod
    def _keep(row: Any) -> bool:
        get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
        name = get("name") or ""
        if not name or name.startswith("_") or get("is_test"):
            return False
        if (get("visibility") or "").lower() in {"private", "protected", "internal", "module"}:
            return False
        if (get("language") or "").lower() == "vue":
            # The Vue surface is the component itself; setup() internals are
            # noise here (page extraction owns that layer).
            return False
        if not _is_public_path(get("file_path") or ""):
            return False
        if (get("language") or "").lower() in EXPORT_FLAG_LANGUAGES:
            return bool(get("is_exported"))
        return True
