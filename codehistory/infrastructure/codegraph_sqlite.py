"""CodeGraph SQLite reader — queries CodeGraph's knowledge graph directly.

Replaces parser.py and crossfile.py entirely.  Cross-file call resolution,
import resolution, and type inference are already done by CodeGraph at
index time — this module only reads the results.

Schema reference (CodeGraph 0.9.x):
  nodes (id, kind, name, qualified_name, file_path, language,
         start_line, end_line, start_column, end_column,
         docstring, signature, visibility,
         is_exported, is_async, is_static, is_abstract,
         decorators, type_parameters, updated_at)
  edges (id, source, target, kind, metadata, line, col, provenance)
  files (path, content_hash, language, size, modified_at, indexed_at, node_count)
"""

import json
import sqlite3
from typing import Any

from ..domain.knowledge import CallTarget, EntryPointDef, FunctionDef

HTTP_DECORATORS = {
    # Python
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "head": "HEAD",
    "options": "OPTIONS",
    "route": None,  # ambiguous — could be any method
    # Java
    "getmapping": "GET",
    "postmapping": "POST",
    "putmapping": "PUT",
    "deletemapping": "DELETE",
    "patchmapping": "PATCH",
    "requestmapping": None,
    # JS/TS use the same lower-cased method names as Python.
    "all": None,
}

HTTP_DIR_PATTERNS = (
    "controller",
    "view",
    "handler",
    "route",
    "api",
    "endpoint",
    "resource",
    "router",
)

TEST_PATTERNS = (
    "/tests/",
    "/test/",
    "/__tests__/",
    "/spec/",
    "/specs/",
    "/fixtures/",
    "/e2e/",
    "/integration/",
    "/__mocks__/",
    ".test.",
    ".spec.",
    "test_",
    "_test.",
)


class _SQLiteCodeGraphQueries:
    """Reads code structure from CodeGraph's SQLite database.

    Usage:
        reader = SQLiteCodeGraphRepository("/path/to/.codegraph/codegraph.db")
        funcs = reader.get_functions_in_file("src/server.py")
        callers = reader.get_callers("0xabc123...")
        callees = reader.get_callees("0xabc123...")
        entry_points = reader.get_entry_points()
        tree = reader.get_call_tree("0xabc123...", max_depth=10)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def query(self, sql: str, params=None) -> list[dict[str, Any]]:
        """Compatibility query API; prefer the repository port in new code."""
        rows = self.conn.execute(sql, params or []).fetchall()
        return [dict(row) for row in rows]

    # ---- Function queries ----

    def get_functions_in_file(self, filepath: str) -> list[FunctionDef]:
        """All functions/methods defined in a file, ordered by start_line."""
        rows = self.conn.execute(
            """SELECT id, kind, name, qualified_name, file_path, language,
                      start_line, end_line, signature, visibility,
                      is_exported, is_async, is_static,
                      decorators
               FROM nodes
               WHERE file_path = ? AND kind IN ('function', 'method')
               ORDER BY start_line""",
            (filepath,),
        ).fetchall()
        return [self._row_to_func(r) for r in rows]

    def get_all_functions(self) -> list[FunctionDef]:
        """All functions/methods in the entire codebase."""
        rows = self.conn.execute(
            """SELECT id, kind, name, qualified_name, file_path, language,
                      start_line, end_line, signature, visibility,
                      is_exported, is_async, is_static,
                      decorators
               FROM nodes
               WHERE kind IN ('function', 'method')
               ORDER BY file_path, start_line"""
        ).fetchall()
        return [self._row_to_func(r) for r in rows]

    def get_function_by_qname(self, qualified_name: str) -> FunctionDef | None:
        """Find a function by its qualified_name."""
        row = self.conn.execute(
            """SELECT id, kind, name, qualified_name, file_path, language,
                      start_line, end_line, signature, visibility,
                      is_exported, is_async, is_static,
                      decorators
               FROM nodes
               WHERE qualified_name = ? AND kind IN ('function', 'method')""",
            (qualified_name,),
        ).fetchone()
        return self._row_to_func(row) if row else None

    def get_function_by_id(self, node_id: str) -> FunctionDef | None:
        """Find a function by its node id."""
        row = self.conn.execute(
            """SELECT id, kind, name, qualified_name, file_path, language,
                      start_line, end_line, signature, visibility,
                      is_exported, is_async, is_static,
                      decorators
               FROM nodes WHERE id = ?""",
            (node_id,),
        ).fetchone()
        return self._row_to_func(row) if row else None

    def _row_to_func(self, row: sqlite3.Row) -> FunctionDef:
        """Convert a SQLite row to a FunctionDef."""
        name = row["name"]
        qualified = row["qualified_name"]
        file_path = row["file_path"]

        # Extract parent_class from qualified_name: "src/file.py::ClassName.method"
        parent_class = None
        if "::" in qualified:
            func_part = qualified.split("::")[-1]
            if "." in func_part:
                parent_class = func_part.rsplit(".", 1)[0]

        # Test file detection
        is_test = (
            any(p in file_path.lower() for p in TEST_PATTERNS)
            or name.lower().startswith("test")
            or name.lower().endswith("test")
        )

        decorators = []
        raw = row["decorators"]
        if raw:
            try:
                decorators = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

        return FunctionDef(
            node_id=row["id"],
            name=name,
            qualified_name=qualified,
            file_path=file_path,
            language=row["language"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            kind=row["kind"],
            signature=row["signature"],
            visibility=row["visibility"],
            is_exported=bool(row["is_exported"]),
            is_async=bool(row["is_async"]),
            is_static=bool(row["is_static"]),
            is_test=is_test,
            parent_class=parent_class,
            decorators=decorators,
        )

    # ---- Call queries ----

    def get_callers(self, node_id: str) -> list[CallTarget]:
        """All functions that call this function (incoming calls)."""
        rows = self.conn.execute(
            """SELECT e.source AS caller_node_id, e.target AS callee_node_id,
                      n.name AS callee_name, n.kind AS callee_kind,
                      n.file_path AS callee_file, n.start_line AS callee_line,
                      e.line AS call_line, e.provenance
               FROM edges e
               JOIN nodes n ON n.id = e.source
               WHERE e.target = ? AND e.kind = 'calls'
               ORDER BY n.file_path, n.start_line""",
            (node_id,),
        ).fetchall()
        return [CallTarget(**dict(r)) for r in rows]

    def get_callees(self, node_id: str) -> list[CallTarget]:
        """All functions called by this function (outgoing calls)."""
        rows = self.conn.execute(
            """SELECT e.source AS caller_node_id, e.target AS callee_node_id,
                      n.name AS callee_name, n.kind AS callee_kind,
                      n.file_path AS callee_file, n.start_line AS callee_line,
                      e.line AS call_line, e.provenance
               FROM edges e
               JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'calls'
               ORDER BY n.file_path, n.start_line""",
            (node_id,),
        ).fetchall()
        return [CallTarget(**dict(r)) for r in rows]

    def get_call_tree(self, node_id: str, max_depth: int = 10) -> list[str]:
        """BFS traversal from entry point — returns node_ids in visit order."""
        visited: set[str] = set()
        order: list[str] = []
        queue = [(node_id, 0)]
        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            order.append(current)
            if depth >= max_depth:
                continue
            callees = self.get_callees(current)
            for c in callees:
                if c.callee_node_id not in visited:
                    queue.append((c.callee_node_id, depth + 1))
        return order

    def get_call_tree_depth(self, node_id: str, max_depth: int = 20) -> int:
        """Maximum depth of call tree from this node."""
        depth: dict[str, int] = {node_id: 1}
        queue = [node_id]
        max_d = 1
        while queue:
            current = queue.pop(0)
            callees = self.get_callees(current)
            for c in callees:
                if c.callee_node_id not in depth:
                    depth[c.callee_node_id] = depth.get(current, 1) + 1
                    max_d = max(max_d, depth[c.callee_node_id])
                    if depth[c.callee_node_id] < max_depth:
                        queue.append(c.callee_node_id)
        return max_d

    def get_call_chain(self, node_id: str, max_depth: int = 10) -> list[dict]:
        """DFS call chain with edge details for visualization.

        Returns [{from, to, depth, file, line, provenance}, ...]
        """
        chain: list[dict] = []
        visited_edges: set[tuple[str, str]] = set()

        def traverse(current: str, depth: int):
            if depth > max_depth:
                return
            callees = self.get_callees(current)
            caller = self.get_function_by_id(current)
            caller_name = caller.name if caller else current
            for c in callees:
                edge_key = (current, c.callee_node_id)
                if edge_key not in visited_edges:
                    visited_edges.add(edge_key)
                    chain.append(
                        {
                            "from": caller_name,
                            "from_node_id": current,
                            "to": c.callee_name,
                            "to_node_id": c.callee_node_id,
                            "depth": depth,
                            "file": c.callee_file,
                            "line": c.call_line,
                            "provenance": c.provenance,
                        }
                    )
                    traverse(c.callee_node_id, depth + 1)

        traverse(node_id, 1)
        return chain

    # ---- Entry point detection ----

    def get_entry_points(
        self,
        filepath: str | None = None,
        exclude_tests: bool = True,
    ) -> list[EntryPointDef]:
        """Detect application entry points from CodeGraph's data.

        Uses decorator annotations (CodeGraph extracts these from AST) for
        accurate HTTP method detection, plus naming/path heuristics as fallback.
        """
        where = "WHERE kind IN ('function', 'method')"
        params: list[Any] = []
        if filepath:
            where += " AND file_path = ?"
            params.append(filepath)

        rows = self.conn.execute(
            f"""SELECT id, kind, name, qualified_name, file_path, language,
                       start_line, end_line, signature, visibility,
                       is_exported, is_async, is_static,
                       decorators
                FROM nodes {where}
                ORDER BY file_path, start_line""",
            params,
        ).fetchall()

        entry_points = []
        for row in rows:
            func = self._row_to_func(row)
            if exclude_tests and func.is_test:
                continue

            result = self._classify_entry(func)
            if result:
                entry_points.append(result)

        return entry_points

    def get_entry_points_in_file(self, filepath: str) -> list[EntryPointDef]:
        """Entry points in a specific file."""
        return self.get_entry_points(filepath=filepath)

    def _classify_entry(self, func: FunctionDef) -> EntryPointDef | None:
        """Classify a function as an entry point based on decorators + naming."""
        name_lower = func.name.lower()
        file_lower = func.file_path.lower()
        decorators_lower = [d.lower() for d in func.decorators]

        # ---- Decorator-based detection (highest confidence) ----
        for deco in decorators_lower:
            # Strip namespace: @app.get → get, @router.post → post
            deco_name = deco.lstrip("@").split(".")[-1]

            # HTTP decorators
            if deco_name in HTTP_DECORATORS:
                method = HTTP_DECORATORS[deco_name]
                # Try to extract path from the decorator (CodeGraph stores full text)
                # For now use the function name as path hint
                return EntryPointDef(
                    node_id=func.node_id,
                    name=func.name,
                    qualified_name=func.qualified_name,
                    file_path=func.file_path,
                    start_line=func.start_line,
                    entry_type="http",
                    http_method=method,
                    params=[],
                    decorators=func.decorators,
                )

            # Event handlers
            if "event" in deco_name or "subscribe" in deco_name or "listen" in deco_name:
                return EntryPointDef(
                    node_id=func.node_id,
                    name=func.name,
                    qualified_name=func.qualified_name,
                    file_path=func.file_path,
                    start_line=func.start_line,
                    entry_type="event",
                    params=[],
                    decorators=func.decorators,
                )

            # Scheduled/cron
            if any(k in deco_name for k in ("scheduled", "cron", "interval", "timeout")):
                return EntryPointDef(
                    node_id=func.node_id,
                    name=func.name,
                    qualified_name=func.qualified_name,
                    file_path=func.file_path,
                    start_line=func.start_line,
                    entry_type="cron",
                    params=[],
                    decorators=func.decorators,
                )

        # ---- Path-based heuristics ----
        is_in_http_dir = any(p in file_lower for p in HTTP_DIR_PATTERNS)

        # HTTP handler naming patterns
        is_http_named = any(
            name_lower.startswith(p)
            for p in ("get_", "post_", "put_", "delete_", "patch_", "head_")
        )

        if is_http_named or (is_in_http_dir and func.is_exported):
            return EntryPointDef(
                node_id=func.node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                start_line=func.start_line,
                entry_type="http",
                params=[],
                decorators=func.decorators,
            )

        # CLI main function
        if func.name == "main":
            return EntryPointDef(
                node_id=func.node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                start_line=func.start_line,
                entry_type="cli",
                params=[],
                decorators=func.decorators,
            )

        # CLI command patterns
        if name_lower.startswith(("cmd_", "cli_", "run_")):
            return EntryPointDef(
                node_id=func.node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                start_line=func.start_line,
                entry_type="cli",
                params=[],
                decorators=func.decorators,
            )

        # Event/callback patterns
        if any(p in name_lower for p in ("_handler", "_callback", "_listener", "_consumer")):
            return EntryPointDef(
                node_id=func.node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                start_line=func.start_line,
                entry_type="event",
                params=[],
                decorators=func.decorators,
            )

        # Vue composables (useXxx pattern)
        if func.name.startswith("use") and len(func.name) > 3 and func.name[3].isupper():
            return EntryPointDef(
                node_id=func.node_id,
                name=func.name,
                qualified_name=func.qualified_name,
                file_path=func.file_path,
                start_line=func.start_line,
                entry_type="other",
                params=[],
                decorators=func.decorators,
            )

        # Export default from pages/ or app/ (Next.js / modern frameworks)
        if func.is_exported and func.name == "default":
            if "pages/" in file_lower or "app/" in file_lower:
                return EntryPointDef(
                    node_id=func.node_id,
                    name=func.name,
                    qualified_name=func.qualified_name,
                    file_path=func.file_path,
                    start_line=func.start_line,
                    entry_type="http",
                    params=[],
                    decorators=func.decorators,
                )

        return None

    # ---- Class/inheritance queries ----

    def get_class_methods(self, class_name: str) -> list[FunctionDef]:
        """All methods of a class (including inherited, via extends/implements edges)."""
        # Find the class node
        class_row = self.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind = 'class'",
            (class_name,),
        ).fetchone()
        if not class_row:
            return []

        class_id = class_row["id"]
        # Methods directly contained in this class
        rows = self.conn.execute(
            """SELECT n.id, n.kind, n.name, n.qualified_name, n.file_path,
                      n.language, n.start_line, n.end_line, n.signature,
                      n.visibility, n.is_exported, n.is_async, n.is_static,
                      n.decorators
               FROM edges e JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'contains' AND n.kind = 'method'
               ORDER BY n.start_line""",
            (class_id,),
        ).fetchall()
        return [self._row_to_func(r) for r in rows]

    def get_class_hierarchy(self, class_name: str) -> dict:
        """Get the full inheritance chain for a class.

        Returns {class_name, extends: [...], implements: [...], methods: [...]}
        """
        class_row = self.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind = 'class'",
            (class_name,),
        ).fetchone()
        if not class_row:
            return {"class_name": class_name, "extends": [], "implements": [], "methods": []}

        class_id = class_row["id"]

        extends = self.conn.execute(
            """SELECT n.name FROM edges e JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'extends'""",
            (class_id,),
        ).fetchall()

        implements = self.conn.execute(
            """SELECT n.name FROM edges e JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'implements'""",
            (class_id,),
        ).fetchall()

        return {
            "class_name": class_name,
            "class_id": class_id,
            "extends": [r["name"] for r in extends],
            "implements": [r["name"] for r in implements],
            "methods": self.get_class_methods(class_name),
        }

    # ---- File queries ----

    def get_all_files(self) -> list[str]:
        """All tracked file paths."""
        rows = self.conn.execute("SELECT path FROM files ORDER BY path").fetchall()
        return [r["path"] for r in rows]

    def get_files_changed_since(self, timestamp_ms: int) -> list[str]:
        """Files whose content changed since a timestamp."""
        rows = self.conn.execute(
            "SELECT path FROM files WHERE modified_at > ? OR indexed_at < modified_at",
            (timestamp_ms,),
        ).fetchall()
        return [r["path"] for r in rows]

    def is_file_indexed(self, filepath: str) -> bool:
        """Check if a file is in CodeGraph's index."""
        row = self.conn.execute("SELECT 1 FROM files WHERE path = ?", (filepath,)).fetchone()
        return row is not None

    # ---- Stats ----

    def stats(self) -> dict:
        """Basic graph statistics."""
        nodes = self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
        edges = self.conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()
        files = self.conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()
        return {
            "total_nodes": nodes["c"],
            "total_edges": edges["c"],
            "total_files": files["c"],
        }


class SQLiteCodeGraphRepository(_SQLiteCodeGraphQueries):
    """The only adapter allowed to access CodeGraph SQLite."""

    def functions(self) -> list[FunctionDef]:
        return self.get_all_functions()

    def callers(self, node_id: str) -> list[CallTarget]:
        return self.get_callers(node_id)

    def callees(self, node_id: str) -> list[CallTarget]:
        return self.get_callees(node_id)

    def inbound_endpoints(self) -> list[EntryPointDef]:
        return self.get_entry_points()

    def route_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id, name, qualified_name, file_path, start_line FROM nodes WHERE kind = 'route'"
        )

    def handler_for_route(self, file_path: str, route_line: int) -> dict[str, Any] | None:
        rows = self.query(
            """SELECT id, kind, name, qualified_name, file_path, start_line, end_line,
                      signature, decorators
               FROM nodes
               WHERE file_path = ? AND kind IN ('function', 'method')
                 AND start_line <= ? AND end_line >= ?
               ORDER BY ABS(start_line - ?) ASC LIMIT 1""",
            [file_path, route_line, route_line, route_line],
        )
        if rows:
            return rows[0]
        rows = self.query(
            """SELECT id, kind, name, qualified_name, file_path, start_line, end_line,
                      signature, decorators
               FROM nodes
               WHERE file_path = ? AND kind IN ('function', 'method')
                 AND ABS(start_line - ?) <= 3
               ORDER BY ABS(start_line - ?) ASC LIMIT 1""",
            [file_path, route_line, route_line],
        )
        return rows[0] if rows else None

    def api_call_chain(self, node_id: str, limit: int = 30) -> list[dict[str, Any]]:
        node_ids = self.get_call_tree(node_id, max_depth=8)[:limit]
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        rows = self.query(
            f"""SELECT id, name, qualified_name, file_path, start_line, kind
                FROM nodes WHERE id IN ({placeholders})""",
            node_ids,
        )
        by_id = {row["id"]: row for row in rows}
        return [by_id[node] for node in node_ids if node in by_id]

    def api_call_chain_mermaid(self, node_id: str, limit: int = 30) -> str:
        """Generate Mermaid flowchart DSL for the API call chain."""
        edges = self.get_call_chain(node_id, max_depth=8)[:limit]
        if not edges:
            return "graph TD\n    N0[\"No downstream calls\"]"

        alias_map: dict[str, str] = {}
        lines = ["graph TD"]

        for edge in edges:
            for name in (edge["from"], edge["to"]):
                if name not in alias_map:
                    alias_map[name] = f"N{len(alias_map)}"
                    safe = name.replace('"', "'").replace("\n", " ")
                    lines.append(f"    {alias_map[name]}[\"{safe}\"]")
            lines.append(f"    {alias_map[edge['from']]} --> {alias_map[edge['to']]}")

        return "\n".join(lines)

    def type_schema(self, type_name: str) -> dict[str, Any] | None:
        rows = self.query(
            """SELECT id, name, qualified_name, file_path, kind, decorators
               FROM nodes WHERE name = ? AND kind IN ('class','interface','record','struct','type')
               ORDER BY CASE WHEN file_path LIKE '%/domain/%' OR file_path LIKE '%/model/%'
                             OR file_path LIKE '%/dto/%' OR file_path LIKE '%/param/%'
                        THEN 0 ELSE 1 END LIMIT 1""",
            [type_name],
        )
        if not rows:
            return None
        model = rows[0]
        fields = self.query(
            """SELECT n.name, n.signature, n.kind
               FROM edges e JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'contains'
                 AND n.kind IN ('field','property','variable') ORDER BY n.start_line""",
            [model["id"]],
        )
        return {
            "name": model["name"],
            "qualified_name": model["qualified_name"],
            "file": model["file_path"],
            "fields": fields[:100],
        }

    def domain_type_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, kind, name, qualified_name, file_path, start_line, decorators
               FROM nodes
               WHERE kind IN ('class','interface','record','struct','type','enum')
                 AND file_path NOT LIKE '%/test/%' AND file_path NOT LIKE '%/tests/%'"""
        )

    def domain_type_relationships(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT e.source, e.target, e.kind
               FROM edges e JOIN nodes s ON s.id=e.source JOIN nodes t ON t.id=e.target
               WHERE s.kind IN ('class','interface','record','struct','type','enum')
                 AND t.kind IN ('class','interface','record','struct','type','enum')
                 AND e.kind IN ('references','type_of','extends','implements')"""
        )

    def domain_type_fields(self, node_id: str) -> list[dict[str, Any]]:
        """Return the fields/properties of a domain type (class/interface/struct)."""
        return self.query(
            """SELECT n.name, n.kind, n.signature, n.start_line
               FROM edges e JOIN nodes n ON n.id = e.target
               WHERE e.source = ? AND e.kind = 'contains'
                 AND n.kind IN ('field','property','variable','enum_member')
               ORDER BY n.start_line""",
            [node_id],
        )

    def domain_type_field_counts(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT e.source AS id, COUNT(*) AS field_count
               FROM edges e JOIN nodes n ON n.id=e.target
               WHERE e.kind='contains' AND n.kind IN ('field','property','variable','enum_member')
               GROUP BY e.source"""
        )

    def decorated_handlers(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, qualified_name, file_path, start_line, signature, decorators
               FROM nodes WHERE kind IN ('function', 'method') AND decorators IS NOT NULL"""
        )

    def module_import_edges(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.file_path AS f1, n2.file_path AS f2 FROM edges e
               JOIN nodes n1 ON n1.id = e.source JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'imports'"""
        )

    def cross_file_call_edges(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.file_path AS f1, n2.file_path AS f2 FROM edges e
               JOIN nodes n1 ON n1.id = e.source JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'calls' AND n1.file_path != n2.file_path"""
        )

    def call_edges(self) -> list[dict[str, Any]]:
        return self.query("SELECT source, target FROM edges WHERE kind = 'calls'")

    def layer_call_edges(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.file_path AS source_file, n1.name AS source_name,
                      n2.file_path AS target_file, n2.name AS target_name, e.line AS call_line
               FROM edges e JOIN nodes n1 ON n1.id = e.source
               JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'calls' AND n1.file_path != n2.file_path"""
        )

    def file_records(self) -> list[dict[str, Any]]:
        return self.query("SELECT path, language FROM files")

    def config_candidate_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, qualified_name, file_path, kind, start_line, decorators
               FROM nodes WHERE kind IN ('variable', 'constant', 'function', 'method')"""
        )

    def import_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT name AS import_name, file_path AS importer_file, start_line, signature
               FROM nodes WHERE kind = 'import'"""
        )

    def decorator_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT DISTINCT decorators, file_path, start_line FROM nodes WHERE decorators IS NOT NULL"
        )

    def authorization_handlers(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, qualified_name, file_path, start_line, decorators, kind
               FROM nodes WHERE kind IN ('function', 'method') AND decorators IS NOT NULL"""
        )

    def authorization_middleware(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, qualified_name, file_path, start_line, kind FROM nodes
               WHERE kind IN ('function', 'method') AND (name LIKE '%auth%middleware%'
               OR name LIKE '%auth%guard%' OR name LIKE '%permission%check%'
               OR name LIKE '%authorize%' OR name LIKE '%authenticate%')"""
        )

    def enum_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, qualified_name, file_path, start_line, end_line
               FROM nodes WHERE kind = 'enum'"""
        )

    def enum_members(self, enum_id: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n.name FROM edges e JOIN nodes n ON n.id=e.target
               WHERE e.source=? AND e.kind='contains' AND n.kind='enum_member'""",
            [enum_id],
        )

    def functions_named_like(self, name: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT DISTINCT qualified_name AS name, file_path, start_line, end_line
               FROM nodes WHERE name LIKE ? AND kind IN ('function','method')""",
            [f"%{name}%"],
        )

    def database_call_candidates(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT DISTINCT caller.qualified_name AS function,
                      target.qualified_name AS target, target.name, target.signature, e.metadata
               FROM edges e JOIN nodes caller ON caller.id=e.source
               JOIN nodes target ON target.id=e.target WHERE e.kind='calls'"""
        )

    def primary_language(self) -> str:
        rows = self.query(
            """SELECT language, COUNT(*) AS count FROM files WHERE language != 'unknown'
               GROUP BY language ORDER BY count DESC LIMIT 1"""
        )
        return rows[0]["language"] if rows else ""

    def has_node_name(self, pattern: str) -> bool:
        return bool(self.query("SELECT 1 FROM nodes WHERE name LIKE ? LIMIT 1", [f"%{pattern}%"]))

    def http_client_calls(self, pattern: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.name AS caller_name, n1.qualified_name AS caller_qname,
                      n1.file_path, n1.start_line AS caller_line, n2.name AS callee_name,
                      e.line AS call_line FROM edges e
               JOIN nodes n1 ON n1.id = e.source JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'calls' AND n2.name LIKE ?""",
            [f"%{pattern}%"],
        )

    def function_location(self, qualified_name: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT file_path, start_line, end_line FROM nodes
               WHERE qualified_name = ? AND kind IN ('function', 'method')""",
            [qualified_name],
        )

    def url_candidate_nodes(
        self, file_path: str, start_line: int, end_line: int
    ) -> list[dict[str, Any]]:
        return self.query(
            """SELECT name, start_line FROM nodes WHERE file_path = ?
               AND start_line BETWEEN ? AND ? AND kind IN ('variable', 'constant')
               AND (name LIKE '%http%' OR name LIKE '%://%' OR name LIKE '%/api/%'
                    OR name LIKE '%base_url%' OR name LIKE '%endpoint%'
                    OR name LIKE '%host%' OR name LIKE '%service_url%')
               ORDER BY start_line""",
            [file_path, start_line, end_line],
        )

    def mq_producer_calls(self, pattern: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.qualified_name AS caller, n1.name, n1.file_path, n1.start_line,
                      e.line AS call_line, n2.name AS callee_name FROM edges e
               JOIN nodes n1 ON n1.id = e.source JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'calls' AND n2.name LIKE ?""",
            [f"%{pattern}%"],
        )

    def mq_consumers(self, pattern: str) -> list[dict[str, Any]]:
        wildcard = f"%{pattern}%"
        return self.query(
            """SELECT n1.qualified_name, n1.name, n1.decorators, n1.file_path, n1.start_line
               FROM nodes n1 WHERE n1.kind IN ('function', 'method')
               AND (n1.name LIKE ? OR n1.decorators LIKE ?)""",
            [wildcard, wildcard],
        )

    def topic_candidate_nodes(self, file_path: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT name FROM nodes WHERE file_path = ? AND kind IN ('variable', 'constant')
               AND (name LIKE '%topic%' OR name LIKE '%queue%' OR name LIKE '%TOPIC%'
                    OR name LIKE '%QUEUE%' OR name LIKE '%exchange%' OR name LIKE '%EXCHANGE%')
               LIMIT 10""",
            [file_path],
        )

    def rpc_calls(self, pattern: str) -> list[dict[str, Any]]:
        return self.query(
            """SELECT n1.qualified_name AS caller, n1.file_path, n1.start_line,
                      n2.name AS callee_name, e.line AS call_line FROM edges e
               JOIN nodes n1 ON n1.id = e.source JOIN nodes n2 ON n2.id = e.target
               WHERE e.kind = 'calls' AND n2.name LIKE ? LIMIT 20""",
            [f"%{pattern}%"],
        )

    def business_entity_nodes(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT name, kind, qualified_name, file_path FROM nodes
               WHERE kind IN ('class', 'struct', 'interface', 'enum', 'type_alias')
                 AND name NOT LIKE 'test%' AND name NOT LIKE 'Test%'
                 AND name NOT LIKE '%Test' AND name NOT LIKE '%Tests'
                 AND file_path NOT LIKE '%test%' AND file_path NOT LIKE '%Test%'
               ORDER BY name"""
        )

    def inspect_metadata(self) -> dict[str, Any]:
        """Return registry metadata while tolerating older CodeGraph schemas."""
        tables = {
            row["name"] for row in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not {"files", "nodes"}.issubset(tables):
            return {
                "language": "",
                "imports": [],
                "nodes": 0,
                "edges": 0,
                "files": 0,
                "indexed_at": None,
                "stale": False,
            }
        file_columns = {row["name"] for row in self.query("PRAGMA table_info(files)")}
        language = ""
        if "language" in file_columns:
            rows = self.query("""SELECT language, COUNT(*) AS count FROM files
                WHERE language IS NOT NULL AND language != 'unknown'
                GROUP BY language ORDER BY count DESC LIMIT 1""")
            language = rows[0]["language"] if rows else ""
        node_columns = {row["name"] for row in self.query("PRAGMA table_info(nodes)")}
        signature = "signature" if "signature" in node_columns else "NULL AS signature"
        imports = self.query(f"SELECT name, {signature} FROM nodes WHERE kind = 'import'")
        counts = {
            table: self.query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
            if table in tables
            else 0
            for table in ("nodes", "edges", "files")
        }
        indexed_at = (
            self.query("SELECT MAX(indexed_at) AS value FROM files")[0]["value"]
            if "indexed_at" in file_columns
            else None
        )
        stale = (
            self.query("SELECT COUNT(*) AS count FROM files WHERE modified_at > indexed_at")[0][
                "count"
            ]
            > 0
            if {"modified_at", "indexed_at"}.issubset(file_columns)
            else False
        )
        return {
            "language": language,
            "imports": imports,
            **counts,
            "indexed_at": indexed_at,
            "stale": stale,
        }


def read_rows(db_path: str, sql: str, params=None) -> list[dict[str, Any]]:
    """Execute a read-only query with deterministic connection cleanup."""
    with SQLiteCodeGraphRepository(db_path) as repository:
        return repository.query(sql, params)
