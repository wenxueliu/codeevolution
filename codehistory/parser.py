"""Tree-sitter based code parser.

Extracts symbols (functions, classes, methods) and edges (calls, imports)
to build call trees from entry points.
"""

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser, Node, Query
from tree_sitter_language_pack import get_language

# Language cache
_LANGUAGES: dict[str, Language] = {}


def _get_language(lang: str) -> Language:
    if lang not in _LANGUAGES:
        _LANGUAGES[lang] = get_language(lang)
    return _LANGUAGES[lang]


def detect_language(filepath: str) -> str | None:
    """Detect language from file extension."""
    ext = Path(filepath).suffix.lower()
    if ext in (".py", ".pyw"):
        return "python"
    if ext in (".java"):
        return "java"
    return None


@dataclass
class FunctionNode:
    """A function or method in the call graph."""
    name: str
    qualified_name: str  # e.g. "module.ClassName.method"
    file_path: str
    line_start: int
    line_end: int
    language: str
    is_method: bool = False
    parent_class: str | None = None
    params: list[str] = field(default_factory=list)
    return_type: str | None = None
    is_test: bool = False


@dataclass
class CallEdge:
    """A call from one function to another."""
    caller: str  # qualified_name of caller
    callee_name: str  # simple name of callee (may be unresolved)
    line: int
    is_resolved: bool = False
    resolved_to: str | None = None  # qualified_name if resolved


@dataclass
class ParsedFile:
    """Result of parsing a single file."""
    file_path: str
    language: str
    functions: list[FunctionNode]
    calls: list[CallEdge]
    imports: list[str]  # import strings
    entry_points: list[FunctionNode]  # functions that are entry points


class SnapshotParser:
    """Parses source files using tree-sitter and builds symbol tables."""

    def __init__(self):
        self._parsed_cache: dict[str, ParsedFile] = {}

    def parse_file(self, filepath: str, content: str) -> ParsedFile:
        """Parse a single file and extract functions + calls."""
        cache_key = f"{filepath}:{hash(content)}"
        if cache_key in self._parsed_cache:
            return self._parsed_cache[cache_key]

        lang = detect_language(filepath)
        if lang is None:
            result = ParsedFile(filepath, "unknown", [], [], [], [])
            self._parsed_cache[cache_key] = result
            return result

        tree_sitter_lang = _get_language(lang)
        parser = Parser(tree_sitter_lang)
        source_bytes = content.encode()
        tree = parser.parse(source_bytes)
        root = tree.root_node

        functions = _extract_functions(root, source_bytes, filepath, lang)
        calls = _extract_calls(root, functions, lang)
        imports = _extract_imports(root, lang)
        entry_points = [f for f in functions if _is_entry_point(root, f, lang)]

        result = ParsedFile(filepath, lang, functions, calls, imports, entry_points)
        self._parsed_cache[cache_key] = result
        return result

    def invalidate(self, filepath: str | None = None):
        """Clear cache for a specific file or all files."""
        if filepath:
            keys = [k for k in self._parsed_cache if k.startswith(f"{filepath}:")]
            for k in keys:
                del self._parsed_cache[k]
        else:
            self._parsed_cache.clear()


# --- Python extractors ---

def _extract_functions(root: Node, source: bytes, filepath: str, lang: str) -> list[FunctionNode]:
    """Extract all function/method definitions from the AST."""
    if lang == "python":
        return _extract_python_functions(root, source, filepath)
    elif lang == "java":
        return _extract_java_functions(root, source, filepath)
    return []


def _extract_python_functions(root: Node, source: bytes, filepath: str) -> list[FunctionNode]:
    """Extract Python functions and methods."""
    functions = []
    current_class: str | None = None

    # Collect functions to avoid double-counting (decorated_definition wraps function_definition)
    seen_lines: set[int] = set()

    def walk(node: Node, class_name: str | None = None):
        nonlocal current_class
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                new_class = name_node.text.decode()
                current_class = new_class
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        walk(child, new_class)
                current_class = class_name
                return

        if node.type == "function_definition":
            start_line = node.start_point[0] + 1
            # Skip if already seen (nested inside decorated_definition)
            if start_line in seen_lines:
                return
            seen_lines.add(start_line)

            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = name_node.text.decode()

            # Build qualified name
            if class_name:
                qualified = f"{filepath}::{class_name}.{name}"
            else:
                qualified = f"{filepath}::{name}"

            # Parameters
            params_node = node.child_by_field_name("parameters")
            params = []
            if params_node:
                for child in params_node.children:
                    if child.type == "identifier":
                        params.append(child.text.decode())

            # Return type
            return_type = None
            return_node = node.child_by_field_name("return_type")
            if return_node:
                return_type = return_node.text.decode()

            # Check if test
            is_test = name.startswith("test_") or name.startswith("test")

            end_line = node.end_point[0] + 1

            functions.append(FunctionNode(
                name=name,
                qualified_name=qualified,
                file_path=filepath,
                line_start=start_line,
                line_end=end_line,
                language="python",
                is_method=class_name is not None,
                parent_class=class_name,
                params=params,
                return_type=return_type,
                is_test=is_test,
            ))

            # Walk nested functions inside this function's body
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    walk(child, class_name)
            return

        if node.type == "decorated_definition":
            for child in node.children:
                walk(child, class_name)
            return

        for child in node.children:
            walk(child, class_name)

    walk(root)
    return functions


def _extract_java_functions(root: Node, source: bytes, filepath: str) -> list[FunctionNode]:
    """Extract Java methods."""
    functions = []
    current_class: str | None = None

    # Find class name
    def find_class(node: Node) -> str | None:
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                return name_node.text.decode()
        for child in node.children:
            result = find_class(child)
            if result:
                return result
        return None

    class_name = find_class(root)

    def walk(node: Node):
        if node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = name_node.text.decode()

            qualified = f"{filepath}::{class_name}.{name}" if class_name else f"{filepath}::{name}"

            params = []
            params_node = node.child_by_field_name("parameters")
            if params_node:
                for child in params_node.children:
                    if child.type == "formal_parameter":
                        id_node = None
                        for c in child.children:
                            if c.type == "identifier":
                                id_node = c
                                break
                        if id_node:
                            params.append(id_node.text.decode())

            return_type = None
            type_node = node.child_by_field_name("type")
            if type_node:
                return_type = type_node.text.decode()

            is_test = name.lower().startswith("test") or (
                class_name and class_name.lower().endswith("test")
            )

            functions.append(FunctionNode(
                name=name,
                qualified_name=qualified,
                file_path=filepath,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                language="java",
                is_method=True,
                parent_class=class_name,
                params=params,
                return_type=return_type,
                is_test=is_test,
            ))

        for child in node.children:
            walk(child)

    walk(root)
    return functions


# --- Call extractors ---

def _extract_calls(root: Node, functions: list[FunctionNode], lang: str) -> list[CallEdge]:
    """Extract function calls from the AST."""
    calls = []
    func_map = {f.name: f.qualified_name for f in functions}
    # Also index by qualified_name for self-resolution
    qualified_map = {f.qualified_name.split("::")[-1]: f.qualified_name for f in functions}

    if lang == "python":
        # Python: call nodes — callee is a child identifier/attribute, not a named field
        def walk_python_calls(node: Node):
            if node.type == "call":
                # Find the callee (identifier or attribute)
                callee = None
                for child in node.children:
                    if child.type in ("identifier", "attribute"):
                        callee = child
                        break
                if callee:
                    callee_name = callee.text.decode()
                    resolved = func_map.get(callee_name)
                    if not resolved:
                        resolved = qualified_map.get(callee_name)
                    enclosing = _find_enclosing_function(node, functions)
                    if enclosing:
                        calls.append(CallEdge(
                            caller=enclosing.qualified_name,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_resolved=resolved is not None,
                            resolved_to=resolved,
                        ))
            for child in node.children:
                walk_python_calls(child)

        walk_python_calls(root)

    elif lang == "java":
        # Java: method_invocation nodes
        def walk_java_calls(node: Node):
            if node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                if name_node:
                    callee_name = name_node.text.decode()
                    resolved = func_map.get(callee_name) or qualified_map.get(callee_name)
                    enclosing = _find_enclosing_function(node, functions)
                    if enclosing:
                        calls.append(CallEdge(
                            caller=enclosing.qualified_name,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_resolved=resolved is not None,
                            resolved_to=resolved,
                        ))
            for child in node.children:
                walk_java_calls(child)

        walk_java_calls(root)

    return calls


def _find_enclosing_function(node: Node, functions: list[FunctionNode]) -> FunctionNode | None:
    """Find which function contains this node."""
    funcs_by_line = sorted(functions, key=lambda f: f.line_start)
    node_line = node.start_point[0] + 1
    for f in funcs_by_line:
        if f.line_start <= node_line <= f.line_end:
            return f
    return None


# --- Import extractors ---

def _extract_imports(root: Node, lang: str) -> list[str]:
    """Extract import statements."""
    imports = []
    if lang == "python":
        for node in _iter_tree(root):
            if node.type in ("import_statement", "import_from_statement"):
                imports.append(node.text.decode())
    elif lang == "java":
        for node in _iter_tree(root):
            if node.type == "import_declaration":
                imports.append(node.text.decode())
    return imports


# --- Entry point detection ---

def _is_entry_point(root: Node, func: FunctionNode, lang: str) -> bool:
    """Check if a function is an application entry point."""
    if lang == "python":
        return _is_python_entry_point(func)
    elif lang == "java":
        return _is_java_entry_point(root, func)
    return False


def _is_python_entry_point(func: FunctionNode) -> bool:
    """Detect Python entry points.

    Recognizes:
    - Flask/FastAPI decorators: @app.get, @app.post, @router.get, etc.
    - CLI: __main__ module functions, click commands, argparse targets
    """
    name = func.name.lower()
    # Web framework patterns (heuristic: name matches)
    web_patterns = (
        "get_", "post_", "put_", "delete_", "patch_",
        "handle_", "view_", "endpoint_", "route_",
    )
    if any(name.startswith(p) for p in web_patterns):
        return True
    # Django view: if function returns HttpResponse (heuristic based on naming)
    if name.endswith("_view") or name.startswith("view_"):
        return True
    return False


def _is_java_entry_point(root: Node, func: FunctionNode) -> bool:
    """Detect Java entry points.

    Recognizes:
    - @RestController, @Controller class methods with @GetMapping, @PostMapping
    - JAX-RS: @Path, @GET, @POST annotations
    - main() methods
    """
    if func.name == "main" and func.params and "String" in str(func.params):
        return True
    if func.name.startswith("get") or func.name.startswith("post") or \
       func.name.startswith("put") or func.name.startswith("delete"):
        return True
    return False


def _iter_tree(node: Node):
    """Iterate all nodes in the AST."""
    yield node
    for child in node.children:
        yield from _iter_tree(child)
