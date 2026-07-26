"""Cross-file call resolution — GitNexus-inspired 4-index + 8-step emit.

Builds workspace-level indexes from all ParsedFiles, then resolves
cross-file calls in emit order (receiver-bound → free → property → fallback).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque


@dataclass
class CrossFileIndex:
    """Four indexes (Finalize mode) + MRO for cross-file call resolution.

    Index 1: defs_by_qname — qualified_name → FunctionNode
    Index 2: scope_tree — class_name → [method qualified_names] + base_classes
    Index 3: module_index — file_path → [FunctionNode]
    Index 4: import_index — (caller_file, alias) → target_file
    """

    # Index 1: all function definitions, keyed by qualified name
    defs_by_qname: dict[str, any] = field(default_factory=dict)
    # Index 1b: simple name → [qualified_name, ...] for fallback lookup
    name_index: dict[str, list[str]] = field(default_factory=dict)

    # Index 2: class → methods + inheritance
    # scope_tree[class_name] = {"methods": [qname, ...], "bases": [base_class, ...]}
    scope_tree: dict[str, dict] = field(default_factory=dict)
    # MRO cache: class_name → [class_name, ...] (linearized)
    _mro_cache: dict[str, list[str]] = field(default_factory=dict)

    # Index 3: file_path → [qualified_names] defined in that file
    module_index: dict[str, list[str]] = field(default_factory=dict)

    # Index 4: import alias → target file
    import_index: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_file(self, parsed):
        """Register all functions, classes, and imports from a parsed file."""
        from .parser import ParsedFile

        fp = parsed.file_path

        # --- Build defs_by_qname + name_index (Index 1) ---
        for func in parsed.functions:
            qname = func.qualified_name
            self.defs_by_qname[qname] = func
            simple = func.name
            if simple not in self.name_index:
                self.name_index[simple] = []
            if qname not in self.name_index[simple]:
                self.name_index[simple].append(qname)

        # --- Build scope_tree (Index 2): class containment ---
        # Python: class_definition → method containment
        # JS/TS: class_declaration → method_definition
        self._build_scope_tree(parsed)

        # --- Build module_index (Index 3) ---
        if fp not in self.module_index:
            self.module_index[fp] = []
        for func in parsed.functions:
            if func.qualified_name not in self.module_index[fp]:
                self.module_index[fp].append(func.qualified_name)

        # --- Build import_index (Index 4) ---
        for imp in parsed.imports:
            self._parse_import(imp, fp)

    def _build_scope_tree(self, parsed):
        """Extract class-method containment from parsed file content.

        Uses the parsed functions' parent_class field to build scope_tree.
        """
        for func in parsed.functions:
            if func.parent_class:
                cls = func.parent_class
                if cls not in self.scope_tree:
                    self.scope_tree[cls] = {"methods": [], "bases": [], "file": parsed.file_path}
                if func.qualified_name not in self.scope_tree[cls]["methods"]:
                    self.scope_tree[cls]["methods"].append(func.qualified_name)

    def add_inheritance(self, class_name: str, base_classes: list[str]):
        """Record class inheritance for MRO computation."""
        if class_name not in self.scope_tree:
            self.scope_tree[class_name] = {"methods": [], "bases": [], "file": ""}
        for base in base_classes:
            if base not in self.scope_tree[class_name]["bases"]:
                self.scope_tree[class_name]["bases"].append(base)

    def compute_mro(self, class_name: str) -> list[str]:
        """Compute C3-linearized MRO for a class.

        Returns [class_name, base1, base2, ..., object] in method resolution order.
        """
        if class_name in self._mro_cache:
            return self._mro_cache[class_name]

        entry = self.scope_tree.get(class_name, {})
        bases = entry.get("bases", [])

        if not bases:
            result = [class_name]
            self._mro_cache[class_name] = result
            return result

        # C3 merge: L(C) = C + merge(L(B1), L(B2), ..., [B1, B2, ...])
        sequences = [self.compute_mro(b) for b in bases if b in self.scope_tree]
        sequences.append(list(bases))

        result = [class_name] + self._c3_merge(sequences)
        self._mro_cache[class_name] = result
        return result

    @staticmethod
    def _c3_merge(sequences: list[list[str]]) -> list[str]:
        """C3 linearization merge."""
        result = []
        while True:
            # Remove empty sequences
            sequences = [s for s in sequences if s]
            if not sequences:
                return result
            # Find a head that doesn't appear in any tail
            found = False
            for seq in sequences:
                head = seq[0]
                # Check if head is in any other sequence's tail
                in_tail = any(head in s[1:] for s in sequences)
                if not in_tail:
                    result.append(head)
                    # Remove head from all sequences
                    for s in sequences:
                        if s and s[0] == head:
                            s.pop(0)
                    found = True
                    break
            if not found:
                # Cycle or unknown base — append remaining heads
                for seq in sequences:
                    if seq and seq[0] not in result:
                        result.append(seq[0])
                return result

    # --- 8-step resolve_call (emit order) ---

    def resolve_call(self, caller_file: str, caller_class: str | None,
                     callee_name: str) -> str | None:
        """Resolve a callee name to a qualified function name.

        Emit order (adapted from GitNexus Contract Invariant I1):
        1. Receiver-bound: self.method() → MRO dispatch
        2. Same-file: callee defined in caller's file
        3. Import-backed single: exactly 1 import candidate
        4. Property dispatch: class-field registration pattern
        5. Free call: global name lookup (evidence-backed)
        6. Same-directory: callee in same directory
        7. Unique global: exactly 1 definition globally

        Args:
            caller_file: The file making the call.
            caller_class: The class containing the caller (None if top-level).
            callee_name: The callee expression (e.g., 'self.get_node', 'module.func').
        """
        cleaned = callee_name.replace("self.", "")

        # --- Step 1: Receiver-bound dispatch ---
        if callee_name.startswith("self."):
            result = self._resolve_receiver_bound(caller_class, cleaned)
            if result:
                return result

        # --- Step 2: Same-file match ---
        result = self._resolve_same_file(caller_file, cleaned)
        if result:
            return result

        # --- Step 3: Dotted call via import ---
        if "." in callee_name and not callee_name.startswith("self."):
            result = self._resolve_dotted_call(caller_file, callee_name)
            if result:
                return result

        # --- Step 4: Property dispatch ---
        result = self._resolve_property_dispatch(caller_file, callee_name)
        if result:
            return result

        # --- Step 5: Import-backed single candidate ---
        result = self._resolve_import_backed(caller_file, cleaned)
        if result:
            return result

        # --- Step 6: Same-directory ---
        result = self._resolve_same_directory(caller_file, cleaned)
        if result:
            return result

        # --- Step 7: Unique global ---
        result = self._resolve_unique_global(cleaned)
        if result:
            return result

        return None

    # --- Step implementations ---

    def _resolve_receiver_bound(self, caller_class: str | None, method_name: str) -> str | None:
        """Step 1: self.method() → walk MRO to find the defining class."""
        if not caller_class:
            return None

        mro = self.compute_mro(caller_class)
        for cls in mro:
            entry = self.scope_tree.get(cls, {})
            for method_qname in entry.get("methods", []):
                # Extract method name from qualified name
                if "::" in method_qname:
                    parts = method_qname.split("::")[-1]  # "ClassName.method"
                    if "." in parts:
                        mn = parts.split(".")[-1]
                        if mn == method_name:
                            return method_qname
                if method_qname.endswith("." + method_name):
                    return method_qname
        return None

    def _resolve_same_file(self, caller_file: str, name: str) -> str | None:
        """Step 2: Callee is defined in the same file."""
        candidates = [
            q for q in self.name_index.get(name, [])
            if q.startswith(caller_file + "::")
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_dotted_call(self, caller_file: str, callee_name: str) -> str | None:
        """Step 3: module.func() → resolve import alias → find function."""
        parts = callee_name.rsplit(".", 1)
        if len(parts) != 2:
            return None
        module_alias, func_name = parts

        target_file = self.import_index.get((caller_file, module_alias))
        if not target_file:
            return None

        # Find func_name in target file's module_index
        candidates = [
            q for q in self.module_index.get(target_file, [])
            if q.endswith("." + func_name) or q.endswith("::" + func_name)
        ]
        # Evidence-backed: only return if exactly 1
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_property_dispatch(self, caller_file: str, callee_name: str) -> str | None:
        """Step 4: obj.handler() pattern where obj is a property/field.

        Try stripping the receiver part: 'obj.method' → look up 'method'.
        Only if 'method' has exactly 1 definition in an imported module.
        """
        if "." not in callee_name:
            return None
        parts = callee_name.rsplit(".", 1)
        if len(parts) != 2:
            return None
        receiver, method = parts

        imported_files = set()
        for (f, alias), target in self.import_index.items():
            if f == caller_file:
                imported_files.add(target)

        candidates = self.name_index.get(method, [])
        evidence_backed = [
            q for q in candidates
            if any(q.startswith(tf + "::") for tf in imported_files)
        ]
        return evidence_backed[0] if len(evidence_backed) == 1 else None

    def _resolve_import_backed(self, caller_file: str, name: str) -> str | None:
        """Step 5: Name has candidates, exactly 1 is from an imported file."""
        candidates = self.name_index.get(name, [])
        if not candidates:
            return None

        imported_files = set()
        for (f, alias), target in self.import_index.items():
            if f == caller_file:
                imported_files.add(target)

        # Also include same-file
        evidence_backed = [
            q for q in candidates
            if q.startswith(caller_file + "::") or
               any(q.startswith(tf + "::") for tf in imported_files)
        ]
        # CRITICAL: exactly 1 (code-review-graph's evidence-backed principle)
        return evidence_backed[0] if len(evidence_backed) == 1 else None

    def _resolve_same_directory(self, caller_file: str, name: str) -> str | None:
        """Step 6: Callee in same directory (weaker evidence)."""
        caller_dir = str(Path(caller_file).parent) + "/"
        candidates = [
            q for q in self.name_index.get(name, [])
            if q.startswith(caller_dir)
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_unique_global(self, name: str) -> str | None:
        """Step 7: Exactly 1 definition globally."""
        candidates = self.name_index.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    # --- Import parsing (unchanged from previous version) ---

    def _parse_import(self, imp_text: str, caller_file: str):
        imp_text = imp_text.strip()
        caller_dir = str(Path(caller_file).parent)
        ext = Path(caller_file).suffix

        if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"):
            self._parse_js_import(imp_text, caller_file, caller_dir)
            return

        if imp_text.startswith("from "):
            parts = imp_text.split(" import ")
            if len(parts) != 2:
                return
            module_path = parts[0].replace("from ", "").strip()
            imports_str = parts[1].strip()
            target_file = self._resolve_module_path(module_path, caller_dir)
            for item in imports_str.split(","):
                item = item.strip()
                if " as " in item:
                    _, alias = item.split(" as ")
                    alias = alias.strip()
                else:
                    alias = item.strip()
                self.import_index[(caller_file, alias)] = target_file

        elif imp_text.startswith("import "):
            module_path = imp_text.replace("import ", "").strip()
            if " as " in module_path:
                module_path, alias = module_path.split(" as ")
                alias = alias.strip()
            else:
                alias = module_path.strip()
            target_file = self._resolve_module_path(module_path.strip(), caller_dir)
            self.import_index[(caller_file, alias)] = target_file

    def _parse_js_import(self, imp_text: str, caller_file: str, caller_dir: str):
        m = re.search(r'''from\s+['\"]([^'\"]+)['\"]''', imp_text)
        if m:
            module_path = m.group(1)
            target_file = self._resolve_js_module(module_path, caller_dir)
            destructure = re.match(r'import\s*\{([^}]+)\}', imp_text)
            if destructure:
                for item in destructure.group(1).split(","):
                    item = item.strip()
                    alias = item.split(" as ")[-1].strip() if " as " in item else item
                    self.import_index[(caller_file, alias)] = target_file
                return
            default_import = re.match(r'import\s+(\w+)', imp_text)
            if default_import and default_import.group(1) not in ("type", "from"):
                self.import_index[(caller_file, default_import.group(1))] = target_file
                return
            namespace = re.match(r'import\s+\*\s+as\s+(\w+)', imp_text)
            if namespace:
                self.import_index[(caller_file, namespace.group(1))] = target_file
                return

        m = re.search(r'''require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)''', imp_text)
        if m:
            target_file = self._resolve_js_module(m.group(1), caller_dir)
            simple = re.match(r'(?:const|let|var)\s+(\w+)\s*=', imp_text)
            if simple:
                self.import_index[(caller_file, simple.group(1))] = target_file
                return
            destructure = re.match(r'(?:const|let|var)\s*\{([^}]+)\}', imp_text)
            if destructure:
                for item in destructure.group(1).split(","):
                    item = item.strip().split(":")[0].strip()
                    self.import_index[(caller_file, item)] = target_file
                return

    def _resolve_module_path(self, module_path: str, caller_dir: str) -> str:
        if module_path.startswith("."):
            dots = len(module_path) - len(module_path.lstrip("."))
            path_parts = module_path.lstrip(".")
            for _ in range(dots - 1):
                caller_dir = str(Path(caller_dir).parent)
            if path_parts:
                return str(Path(caller_dir) / (path_parts.replace(".", "/") + ".py"))
            return str(Path(caller_dir) / "__init__.py")
        return module_path.replace(".", "/") + ".py"

    def _resolve_js_module(self, module_path: str, caller_dir: str) -> str:
        if module_path.startswith("."):
            resolved = str(Path(caller_dir) / module_path)
            return resolved + ".ts"
        return module_path
