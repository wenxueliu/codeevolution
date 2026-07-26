"""Tree-sitter based code parser.

Extracts symbols (functions, classes, methods) and edges (calls, imports)
to build call trees from entry points.
"""

import re
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
    path = Path(filepath)
    ext = path.suffix.lower()
    name = path.name.lower()

    # Dotfiles: .env, .cfg, .gitignore etc.
    if name.startswith(".") and not ext:
        if name in (".env",):
            return "config"
        return None

    if ext in (".py", ".pyw"):
        return "python"
    if ext in (".java"):
        return "java"
    if ext in (".ts"):
        return "typescript"
    if ext in (".tsx"):
        return "tsx"
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    if ext in (".vue"):
        return "vue"
    if ext in (".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties", ".xml"):
        return "config"
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
    imports: list[str]
    entry_points: list[FunctionNode]
    inheritance: list[dict] = field(default_factory=list)  # [{class, bases}]
    config_keys: list[str] = field(default_factory=list)  # config key paths


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

        # Config files: extract key-value pairs, no functions/calls/entry points
        if lang == "config":
            config_keys = _parse_config_keys(filepath, content)
            result = ParsedFile(filepath, lang, [], [], [], [], config_keys=config_keys)
            self._parsed_cache[cache_key] = result
            return result

        # Vue: extract <script> section and parse as ts/js
        if lang == "vue":
            script_content, script_lang = _extract_vue_script(content)
            if not script_content:
                result = ParsedFile(filepath, "vue", [], [], [], [])
                self._parsed_cache[cache_key] = result
                return result
            lang = script_lang  # Use the script's language (ts or js)

        tree_sitter_lang = _get_language(lang)
        parser = Parser(tree_sitter_lang)
        source_bytes = content.encode() if lang != "vue" else script_content.encode()
        tree = parser.parse(source_bytes)
        root = tree.root_node

        functions = _extract_functions(root, source_bytes, filepath, lang)
        calls = _extract_calls(root, functions, lang)
        imports = _extract_imports(root, lang)
        entry_points = [f for f in functions if _is_entry_point(root, f, lang)]
        inheritance = _extract_inheritance(root, content if lang != "vue" else (script_content or content), lang)

        result = ParsedFile(filepath, "vue" if detect_language(filepath) == "vue" else lang,
                           functions, calls, imports, entry_points,
                           inheritance=inheritance)
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
    elif lang in ("javascript", "typescript", "tsx"):
        return _extract_js_functions(root, source, filepath, lang)
    elif lang == "vue":
        return _extract_js_functions(root, source, filepath, "typescript")
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


# --- Vue script extractor ---

def _extract_vue_script(content: str) -> tuple[str, str]:
    """Extract the <script> section from a Vue SFC. Returns (content, lang)."""
    import re
    # Match <script lang="ts"> or <script setup lang="ts"> or plain <script>
    m = re.search(r'<script[^>]*>', content)
    if not m:
        return "", ""
    start = m.end()
    end = content.find("</script>", start)
    if end < 0:
        return "", ""
    tag = m.group(0)
    if 'lang="ts"' in tag or "lang='ts'" in tag or 'lang=ts' in tag:
        lang = "typescript"
    elif 'lang="tsx"' in tag:
        lang = "tsx"
    else:
        lang = "javascript"
    return content[start:end], lang


# --- JS/TS extractors ---

def _extract_js_functions(root: Node, source: bytes, filepath: str, lang: str) -> list[FunctionNode]:
    """Extract JavaScript/TypeScript functions, methods, and arrow functions."""
    functions = []
    current_class: str | None = None

    def get_text(node: Node) -> str:
        return node.text.decode() if node.text else ""

    def walk(node: Node, class_name: str | None = None):
        nonlocal current_class

        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            new_class = get_text(name_node) if name_node else "Anonymous"
            current_class = new_class
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    walk(child, new_class)
            current_class = class_name
            return

        if node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = get_text(name_node)

            qualified = f"{filepath}::{class_name}.{name}" if class_name else f"{filepath}::{name}"

            params = []
            params_node = node.child_by_field_name("parameters")
            if params_node:
                for child in params_node.children:
                    if child.type in ("identifier", "required_parameter", "optional_parameter"):
                        for c in child.children:
                            if c.type == "identifier":
                                params.append(get_text(c))
                                break

            is_test = name.lower().startswith("test") or name.lower().endswith("test") or \
                      (class_name and ("test" in class_name.lower() or "spec" in class_name.lower()))

            functions.append(FunctionNode(
                name=name, qualified_name=qualified, file_path=filepath,
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                language=lang, is_method=class_name is not None,
                parent_class=class_name, params=params, is_test=is_test,
            ))

        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = get_text(name_node)

            qualified = f"{filepath}::{class_name}.{name}" if class_name else f"{filepath}::{name}"

            params = []
            params_node = node.child_by_field_name("parameters")
            if params_node:
                for child in params_node.children:
                    if child.type in ("identifier", "required_parameter", "optional_parameter"):
                        for c in child.children:
                            if c.type == "identifier":
                                params.append(get_text(c))
                                break

            is_test = name.lower().startswith("test") or name.lower().endswith("test")

            functions.append(FunctionNode(
                name=name, qualified_name=qualified, file_path=filepath,
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                language=lang, is_method=False, params=params, is_test=is_test,
            ))

        if node.type in ("arrow_function", "function_expression"):
            # Variable declarator or assignment: const foo = () => { ... }
            parent = node.parent
            if parent:
                if parent.type == "variable_declarator":
                    name_node = parent.child_by_field_name("name")
                    if name_node:
                        name = get_text(name_node)
                        qualified = f"{filepath}::{class_name}.{name}" if class_name else f"{filepath}::{name}"
                        functions.append(FunctionNode(
                            name=name, qualified_name=qualified, file_path=filepath,
                            line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                            language=lang, is_method=False,
                            parent_class=class_name, is_test=name.lower().startswith("test"),
                        ))

        # Unwrap export statements to find inner functions
        if node.type == "export_statement":
            for child in node.children:
                if child.type in ("function_declaration", "class_declaration",
                                  "lexical_declaration", "variable_declaration"):
                    walk(child, class_name)
            return

        # Recurse: find function/class children anywhere in the tree
        for child in node.children:
            walk(child, class_name)

    walk(root)
    return functions


# --- Call extractors ---

def _extract_calls(root: Node, functions: list[FunctionNode], lang: str) -> list[CallEdge]:
    """Extract function calls from the AST."""
    calls = []
    js_langs = ("javascript", "typescript", "tsx")
    func_map = {f.name: f.qualified_name for f in functions}
    # Also index by qualified_name short form: "ClassName.method" and "method"
    qualified_map: dict[str, str] = {}
    for f in functions:
        short = f.qualified_name.split("::")[-1]  # "ClassName.method" or "method"
        qualified_map[short] = f.qualified_name
        qualified_map[f.name] = f.qualified_name  # "method" → qualified

    if lang == "python":
        def _resolve_callee(callee_name: str) -> str | None:
            """Try to resolve a callee name to a qualified function name."""
            # Direct match
            if callee_name in func_map:
                return func_map[callee_name]
            if callee_name in qualified_map:
                return qualified_map[callee_name]
            # Strip 'self.' prefix for method calls
            if callee_name.startswith("self."):
                method_name = callee_name[5:]
                if method_name in func_map:
                    return func_map[method_name]
                if method_name in qualified_map:
                    return qualified_map[method_name]
                return None
            # Object.attr() calls (external objects, builtins) — skip
            if "." in callee_name:
                return None
            return None

        def walk_python_calls(node: Node):
            if node.type == "call":
                callee = None
                for child in node.children:
                    if child.type in ("identifier", "attribute"):
                        callee = child
                        break
                if callee:
                    callee_name = callee.text.decode()
                    resolved_to = _resolve_callee(callee_name)
                    enclosing = _find_enclosing_function(node, functions)
                    if enclosing:
                        calls.append(CallEdge(
                            caller=enclosing.qualified_name,
                            callee_name=callee_name,
                            line=node.start_point[0] + 1,
                            is_resolved=resolved_to is not None,
                            resolved_to=resolved_to,
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

    elif lang in js_langs:
        def walk_js_calls(node: Node):
            if node.type == "call_expression":
                # function() or obj.method()
                fn_node = node.child_by_field_name("function")
                if fn_node:
                    callee_name = fn_node.text.decode() if fn_node.text else ""
                    if callee_name:
                        resolved = func_map.get(callee_name)
                        if not resolved:
                            resolved = qualified_map.get(callee_name)
                        if not resolved and "." in callee_name:
                            # obj.method() → try just the method name
                            method = callee_name.rsplit(".", 1)[-1]
                            resolved = func_map.get(method) or qualified_map.get(method)
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
                walk_js_calls(child)

        walk_js_calls(root)

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
    elif lang in ("javascript", "typescript", "tsx"):
        for node in _iter_tree(root):
            if node.type == "import_statement":
                imports.append(node.text.decode())
            # CommonJS require(): const x = require('...')
            if node.type in ("lexical_declaration", "variable_declaration"):
                text = node.text.decode() if node.text else ""
                if "require(" in text:
                    imports.append(text.strip())
    return imports


# --- Entry point detection ---

def _is_entry_point(root: Node, func: FunctionNode, lang: str) -> bool:
    """Check if a function is an application entry point."""
    if lang == "python":
        return _is_python_entry_point(func)
    elif lang == "java":
        return _is_java_entry_point(root, func)
    elif lang in ("javascript", "typescript", "tsx", "vue"):
        return _is_js_entry_point(func)
    return False


def _is_python_entry_point(func: FunctionNode) -> bool:
    """Detect Python entry points.

    Recognizes:
    - Flask/FastAPI decorators: @app.get, @app.post, @router.get, etc.
    - CLI: __main__ module functions, click commands, argparse targets

    Excludes test functions and functions in test files.
    """
    if func.is_test:
        return False
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
    """Detect Java entry points. Excludes test functions."""
    if func.is_test:
        return False
    if func.name == "main" and func.params and "String" in str(func.params):
        return True
    if func.name.startswith("get") or func.name.startswith("post") or \
       func.name.startswith("put") or func.name.startswith("delete"):
        return True
    return False


def _is_js_entry_point(func: FunctionNode) -> bool:
    """Detect JS/TS/Vue entry points. Excludes test functions."""
    if func.is_test:
        return False
    name = func.name.lower()
    path_lower = func.file_path.lower()

    # HTTP framework patterns
    http_dirs = ("api", "controller", "route", "handler", "endpoint", "service", "middleware")
    if any(d in path_lower for d in http_dirs):
        return True

    # Next.js patterns
    if "pages/" in path_lower or "app/" in path_lower:
        nextjs_exports = ("getserversideprops", "getstaticprops", "getstaticpaths",
                          "generatemetadata", "generatestaticparams")
        if name in nextjs_exports:
            return True
        if name == "default" and ("page" in path_lower or "layout" in path_lower or "route" in path_lower):
            return True

    # Express-like handlers: (req, res) or (request, response) params
    params_lower = [p.lower() for p in func.params]
    if any(p in params_lower for p in ("req", "request", "ctx")):
        return True

    # Vue composables: useXxx (check original name for camelCase)
    if func.name.startswith("use") and len(func.name) > 3 and func.name[3].isupper():
        return True

    # CLI / main entry points
    if name == "main" or name == "cli" or name.startswith("cli_"):
        return True

    # Exported function with handler-like names
    handler_patterns = ("handle", "handler", "route", "endpoint", "controller")
    if any(p in name for p in handler_patterns):
        return True

    # Vue template event handlers: short functions in .vue files (likely @click, @submit etc.)
    if func.file_path.endswith(".vue") and len(func.params) <= 2:
        return True

    return False


def _extract_inheritance(root: Node, source: str, lang: str) -> list[dict]:
    """Extract class inheritance relationships.

    Returns [{class: 'Foo', bases: ['Bar', 'Baz']}, ...]
    """
    result = []
    source_bytes = source.encode() if isinstance(source, str) else source

    if lang == "python":
        # Python: class Foo(Bar, Baz):
        for node in _iter_tree(root):
            if node.type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode()
                    bases = []
                    superclass_node = node.child_by_field_name("superclasses")
                    if superclass_node:
                        for child in superclass_node.children:
                            if child.type == "identifier":
                                bases.append(child.text.decode())
                            elif child.type == "attribute":
                                bases.append(child.text.decode())
                    if bases:
                        result.append({"class": class_name, "bases": bases})

    elif lang in ("javascript", "typescript", "tsx"):
        # JS/TS: class Foo extends Bar implements Baz
        for node in _iter_tree(root):
            if node.type == "class_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode()
                    bases = []
                    for child in node.children:
                        if child.type == "extends_clause":
                            for c in child.children:
                                if c.type == "identifier":
                                    bases.append(c.text.decode())
                        if child.type == "implements_clause":
                            for c in child.children:
                                if c.type == "identifier":
                                    bases.append(c.text.decode())
                    if bases:
                        result.append({"class": class_name, "bases": bases})

    elif lang == "java":
        # Java: class Foo extends Bar implements Baz
        for node in _iter_tree(root):
            if node.type == "class_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode()
                    bases = []
                    superclass = node.child_by_field_name("superclass")
                    if superclass:
                        for c in superclass.children:
                            if c.type == "type_identifier":
                                bases.append(c.text.decode())
                    interfaces = node.child_by_field_name("interfaces")
                    if interfaces:
                        for c in interfaces.children:
                            if c.type == "type_identifier":
                                bases.append(c.text.decode())
                    if bases:
                        result.append({"class": class_name, "bases": bases})

    return result


# --- Config file parser ---

def _parse_config_keys(filepath: str, content: str) -> list[str]:
    """Extract key paths from config files."""
    import json
    path = Path(filepath)
    ext = path.suffix.lower()
    name = path.name.lower()

    try:
        # Dotfiles
        if not ext and name == ".env":
            return _extract_env_keys(content)

        if ext in (".json",):
            return _extract_json_keys(json.loads(content))
        elif ext in (".yml", ".yaml"):
            return _extract_yaml_keys(content)
        elif ext in (".toml"):
            return _extract_toml_keys(content)
        elif ext in (".ini", ".cfg", ".conf"):
            return _extract_ini_keys(content)
        elif ext in (".properties",):
            return _extract_properties_keys(content)
        elif ext in (".xml",):
            return _extract_xml_keys(content)
    except Exception:
        pass
    return []


def _extract_json_keys(data, prefix: str = "") -> list[str]:
    keys = []
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.append(path)
            if isinstance(v, (dict, list)):
                keys.extend(_extract_json_keys(v, path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                keys.extend(_extract_json_keys(item, f"{prefix}[{i}]" if prefix else f"[{i}]"))
    return keys


def _extract_yaml_keys(content: str) -> list[str]:
    """Extract keys from YAML without PyYAML dependency (basic regex)."""
    keys = []
    for line in content.split("\n"):
        # Skip comments and empty lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Match indented key: "  key:" or "key:"
        m = re.match(r'^(\s*)([\w_-]+)\s*:', line)
        if m:
            indent = len(m.group(1))
            key = m.group(2)
            # Build dotted path from indentation level
            depth = indent // 2  # assume 2-space indent
            keys.append(key)  # store bare key; depth info could be used to build path
    return keys


def _extract_toml_keys(content: str) -> list[str]:
    """Extract keys from TOML (regex-based)."""
    keys = []
    current_section = ""
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # [section]
        m = re.match(r'^\[([^\]]+)\]', stripped)
        if m:
            current_section = m.group(1)
            keys.append(current_section)
            continue
        # key = value
        m = re.match(r'^([\w_-]+)\s*=', stripped)
        if m:
            key = m.group(1)
            full_key = f"{current_section}.{key}" if current_section else key
            keys.append(full_key)
    return keys


def _extract_env_keys(content: str) -> list[str]:
    """Extract keys from .env files."""
    keys = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=', stripped)
        if m:
            keys.append(m.group(1))
    return keys


def _extract_ini_keys(content: str) -> list[str]:
    """Extract keys from INI/CFG files."""
    keys = []
    current_section = ""
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        m = re.match(r'^\[([^\]]+)\]', stripped)
        if m:
            current_section = m.group(1)
            keys.append(current_section)
            continue
        m = re.match(r'^([\w_-]+)\s*[=:]', stripped)
        if m:
            key = m.group(1)
            full_key = f"{current_section}.{key}" if current_section else key
            keys.append(full_key)
    return keys


def _extract_properties_keys(content: str) -> list[str]:
    """Extract keys from Java .properties files.

    Format: key=value or key:value, with dot-separated hierarchical keys.
    Lines starting with # or ! are comments.
    """
    keys = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        m = re.match(r'^([\w.\\-]+)\s*[=:]', stripped)
        if m:
            key = m.group(1).strip()
            # Unescape Java properties: \: → :, \= → =
            key = key.replace("\\:", ":").replace("\\=", "=").replace("\\ ", " ")
            keys.append(key)
    return keys


def _extract_xml_keys(content: str) -> list[str]:
    """Extract structured keys from XML config files.

    Extracts:
    - Element names (tag names)
    - Key attributes: name, key, id, property, bean, class
    - Spring-style property paths: <property name="x" value="y"/>
    - Text content for leaf elements
    """
    keys = []

    # Pattern 1: Tag names as hierarchical paths
    tag_stack = []
    for match in re.finditer(r'</?([\w:.-]+)[^>]*>', content):
        tag = match.group(1)
        is_closing = match.group(0).startswith("</")
        is_self_closing = match.group(0).endswith("/>")

        if is_closing:
            if tag_stack and tag_stack[-1] == tag:
                tag_stack.pop()
        else:
            if tag not in ("?xml",):
                tag_stack.append(tag)
                # Build path
                path = ".".join(tag_stack)
                keys.append(path)
            if is_self_closing and tag_stack and tag_stack[-1] == tag:
                tag_stack.pop()

    # Pattern 2: Extract key attributes (Spring config, Maven, etc.)
    for m in re.finditer(r'\b(name|key|id|property|ref|class|file)\s*=\s*"([^"]+)"', content):
        attr_name = m.group(1)
        attr_value = m.group(2)
        keys.append(f"{attr_name}={attr_value}")

    # Pattern 3: Spring property placeholders ${...}
    for m in re.finditer(r'\$\{([^}]+)\}', content):
        keys.append(f"${{{m.group(1)}}}")

    # Deduplicate
    return list(dict.fromkeys(keys))


def _iter_tree(node: Node):
    """Iterate all nodes in the AST."""
    yield node
    for child in node.children:
        yield from _iter_tree(child)
