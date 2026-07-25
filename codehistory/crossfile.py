"""Cross-file call resolution.

Builds a global function index across all files in a commit,
then resolves calls that span file boundaries.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CrossFileIndex:
    """Index for resolving calls across files within a commit."""

    # simple_name → [qualified_name, ...] across all files
    name_index: dict[str, list[str]] = field(default_factory=dict)
    # (caller_file, import_alias) → target_file_path
    import_map: dict[tuple[str, str], str] = field(default_factory=dict)
    # qualified_name → FunctionNode (for easy lookup)
    all_functions: dict[str, any] = field(default_factory=dict)

    def add_file(self, parsed):
        """Register all functions and imports from a parsed file."""
        from .parser import ParsedFile

        # Index functions by simple name
        for func in parsed.functions:
            qname = func.qualified_name
            simple = func.name
            if simple not in self.name_index:
                self.name_index[simple] = []
            self.name_index[simple].append(qname)
            self.all_functions[qname] = func

        # Parse imports for module alias → file mapping
        for imp in parsed.imports:
            self._parse_import(imp, parsed.file_path)

    def _parse_import(self, imp_text: str, caller_file: str):
        """Parse import statement and record alias→file mappings.

        Supports Python and JS/TS import syntax.
        """
        imp_text = imp_text.strip()
        caller_dir = str(Path(caller_file).parent)
        ext = Path(caller_file).suffix

        # JS/TS imports
        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"):
            self._parse_js_import(imp_text, caller_file, caller_dir)
            return

        # Python imports
        if imp_text.startswith("from "):
            # from X import Y, Z
            parts = imp_text.split(" import ")
            if len(parts) != 2:
                return
            module_path = parts[0].replace("from ", "").strip()
            imports_str = parts[1].strip()

            # Resolve the relative module path to a file path
            target_file = self._resolve_module_path(module_path, caller_dir)

            # Parse imported names (handle 'import a, b, c' and 'import a as b')
            for item in imports_str.split(","):
                item = item.strip()
                if " as " in item:
                    original, alias = item.split(" as ")
                    alias = alias.strip()
                else:
                    original = item.strip()
                    alias = original

                self.import_map[(caller_file, alias)] = target_file

        elif imp_text.startswith("import "):
            # import X  or  import X as Y
            module_path = imp_text.replace("import ", "").strip()
            if " as " in module_path:
                module_path, alias = module_path.split(" as ")
                module_path = module_path.strip()
                alias = alias.strip()
            else:
                alias = module_path

            target_file = self._resolve_module_path(module_path, caller_dir)
            self.import_map[(caller_file, alias)] = target_file

    def _parse_js_import(self, imp_text: str, caller_file: str, caller_dir: str):
        """Parse JS/TS import and record alias→file mappings.

        Examples:
            import { foo } from './module'  → alias 'foo' → caller_dir/module.ts
            import foo from './module'      → alias 'foo' → caller_dir/module.ts
            import * as foo from './module' → alias 'foo' → caller_dir/module.ts
            const { foo } = require('./module') → alias 'foo' → caller_dir/module.ts
        """
        import re

        # ES6 imports: import ... from '...'
        m = re.search(r'''from\s+['\"]([^'\"]+)['\"]''', imp_text)
        if m:
            module_path = m.group(1)
            target_file = self._resolve_js_module(module_path, caller_dir)

            # import { foo, bar } from ...
            destructure = re.match(r'import\s*\{([^}]+)\}', imp_text)
            if destructure:
                for item in destructure.group(1).split(","):
                    item = item.strip()
                    if " as " in item:
                        original, alias = item.split(" as ")
                        alias = alias.strip()
                    else:
                        alias = item.strip()
                    self.import_map[(caller_file, alias)] = target_file
                return

            # import foo from ...
            default_import = re.match(r'import\s+(\w+)', imp_text)
            if default_import:
                alias = default_import.group(1)
                if alias not in ("type", "from"):
                    self.import_map[(caller_file, alias)] = target_file
                return

            # import * as foo from ...
            namespace = re.match(r'import\s+\*\s+as\s+(\w+)', imp_text)
            if namespace:
                self.import_map[(caller_file, namespace.group(1))] = target_file
                return

        # CommonJS: const foo = require('...') or const { foo } = require('...')
        m = re.search(r'''require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)''', imp_text)
        if m:
            module_path = m.group(1)
            target_file = self._resolve_js_module(module_path, caller_dir)

            # const foo = require(...)
            simple = re.match(r'(?:const|let|var)\s+(\w+)\s*=', imp_text)
            if simple:
                self.import_map[(caller_file, simple.group(1))] = target_file
                return

            # const { foo, bar } = require(...)
            destructure = re.match(r'(?:const|let|var)\s*\{([^}]+)\}', imp_text)
            if destructure:
                for item in destructure.group(1).split(","):
                    item = item.strip()
                    if ":" in item:
                        item = item.split(":")[0].strip()
                    self.import_map[(caller_file, item)] = target_file
                return

    def _resolve_js_module(self, module_path: str, caller_dir: str) -> str:
        """Convert a JS/TS module path to a file path.

        './utils' → '<caller_dir>/utils'
        '../shared/foo' → '<parent>/shared/foo'
        'lodash' → 'node_modules/lodash' (external, won't match)
        """
        if module_path.startswith("."):
            resolved = str(Path(caller_dir) / module_path)
            # Try common extensions
            for ext in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                if Path(resolved + ext).exists() if hasattr(Path, 'exists') else True:
                    return resolved + ext
            return resolved + ".ts"  # default to .ts
        # External package — return as-is (won't match any file in the repo)
        return module_path

    def _resolve_module_path(self, module_path: str, caller_dir: str) -> str:
        """Convert a Python module path to a file path.

        'server.graph' → 'server/graph.py'
        '.utils' → '<caller_dir>/utils.py'
        """
        if module_path.startswith("."):
            # Relative import
            dots = len(module_path) - len(module_path.lstrip("."))
            path_parts = module_path.lstrip(".")
            for _ in range(dots - 1):
                caller_dir = str(Path(caller_dir).parent) if Path(caller_dir).parent != Path(caller_dir) else caller_dir
            if path_parts:
                return str(Path(caller_dir) / (path_parts.replace(".", "/") + ".py"))
            return str(Path(caller_dir) / "__init__.py")

        # Absolute import: replace dots with slashes
        file_path = module_path.replace(".", "/") + ".py"
        return file_path

    def resolve_call(self, caller_file: str, callee_name: str) -> str | None:
        """Try to resolve a callee name to a qualified function name.

        Args:
            caller_file: The file making the call.
            callee_name: The callee as it appears in the source (e.g., 'get_node', 'module.func').

        Returns:
            Qualified function name or None if unable to resolve.
        """
        # Strategy 1: Direct name lookup (simple name, from import)
        if callee_name in self.name_index:
            candidates = self.name_index[callee_name]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                # Prefer the one that's imported by this file
                for c in candidates:
                    c_file = c.split("::")[0]
                    if (caller_file, callee_name) in self.import_map:
                        if self.import_map[(caller_file, callee_name)] == c_file:
                            return c
                # Fallback: return the first one (might have duplicates across files)
                # For now, prefer same-directory
                caller_dir = str(Path(caller_file).parent)
                for c in candidates:
                    c_file = c.split("::")[0]
                    if str(Path(c_file).parent) == caller_dir:
                        return c
                return candidates[0]  # best-effort

        # Strategy 2: Dotted call (module.func)
        if "." in callee_name:
            parts = callee_name.rsplit(".", 1)
            if len(parts) == 2:
                module_alias, func_name = parts
                # Try exact module.func resolution
                if (caller_file, module_alias) in self.import_map:
                    target_file = self.import_map[(caller_file, module_alias)]
                    # Look up func_name in the target file's exported functions
                    for qname in self.name_index.get(func_name, []):
                        if qname.startswith(target_file + "::"):
                            return qname
                        # Also try without file path prefix (might be relative)
                        if qname.split("::")[0].endswith(target_file.split("/")[-1]):
                            return qname

                # Try just by function name (if func_name is unique across files)
                if func_name in self.name_index:
                    candidates = self.name_index[func_name]
                    if len(candidates) == 1:
                        return candidates[0]

        # Strategy 3: Self-call resolution (self.method → look up method)
        cleaned = callee_name.replace("self.", "")
        if cleaned in self.name_index and cleaned != callee_name:
            candidates = self.name_index[cleaned]
            if len(candidates) == 1:
                return candidates[0]

        return None

    def get_function_file(self, qualified_name: str) -> str:
        """Extract the file path from a qualified function name."""
        return qualified_name.split("::")[0]
