"""Best-effort API-endpoint detection for Python stdlib ``http.server`` handlers.

CodeGraph recognises declarative routing idioms (Flask / FastAPI / Express ...
decorators and route tables) but cannot understand the classic zero-dependency
``http.server`` pattern: a ``BaseHTTPRequestHandler`` subclass whose ``do_GET`` /
``do_POST`` / ... methods hand-dispatch on ``self.path`` with ``if path == ...``,
``if path.startswith(...)``, ``if '...' in path`` and positional
``parts = path.split('/')`` segment checks.

This module reconstructs those endpoints from source with the Python ``ast``.
It is deliberately heuristic — a manual dispatch table is imperative code, not a
declaration — and should be treated as best-effort:

* each handled branch of a ``do_<VERB>`` method that ends in a call to a
  per-endpoint helper (``return self._list_workflows()``) or an inline response
  behind an exact path test becomes one endpoint;
* the URL template is rebuilt from the literal prefixes / suffixes / segment
  tokens in the guard conditions plus the path parameters the helper consumes;
* branches whose guards are self-contradictory (e.g. a literal placed over a
  base segment) are dropped instead of guessing.

All functions here are pure (source string in, specs out) and unit-testable.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

#: HTTP verbs exposed as ``do_GET`` / ``do_POST`` ... on a handler.
HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}

#: handler helpers that just write a response — not per-endpoint actions.
_RESPONSE_HELPERS = {"_send_json", "_send_error", "log_message"}


@dataclass
class EndpointSpec:
    method: str
    path: str
    handler: str
    line: int
    params: list[str] = field(default_factory=list)
    class_name: str = ""
    #: hints about the inference (e.g. coarse fallback).
    note: str = ""
    #: request-payload kind read by the do_* method for this endpoint:
    #: "" (none), "json" (parsed JSON object) or "bytes" (raw payload bytes).
    request_body_kind: str = ""


# --------------------------------------------------------------------------- #
# module / class discovery
# --------------------------------------------------------------------------- #


def handler_class_names(module_ast: ast.Module) -> list[str]:
    """Names of classes that look like ``http.server`` handlers.

    A class is a candidate when it defines at least one ``do_<VERB>`` method.
    """
    names: list[str] = []
    for node in module_ast.body:
        if not isinstance(node, ast.ClassDef):
            continue
        verbs = {
            meth.name[len("do_") :].upper()
            for meth in node.body
            if isinstance(meth, (ast.FunctionDef, ast.AsyncFunctionDef))
            and meth.name.startswith("do_")
        }
        if verbs & HTTP_VERBS:
            names.append(node.name)
    return names


# --------------------------------------------------------------------------- #
# guard-condition analysis -> facts
# --------------------------------------------------------------------------- #


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _int_literal(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        return -node.operand.value
    return None


def _is_parts(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "parts"
    )


def _parts_idx(node: ast.AST) -> int | None:
    """Absolute/relative integer index when ``node`` is ``parts[<int>]``."""
    if _is_parts(node):
        return _int_literal(node.slice)
    return None


def _flatten_and(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out: list[ast.AST] = []
        for value in node.values:
            out.extend(_flatten_and(value))
        return out
    return [node]


def _fact(node: ast.AST) -> dict | None:
    """Turn one guard expression into a routing fact, or ``None``."""
    # `'/x' not in path` / `parts[i] not in {...}`
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _fact(node.operand)
        if inner:
            return {**inner, "neg": True}
        return None

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        op = node.ops[0]
        left, right = node.left, node.comparators[0]
        # literal equality with a path variable: path == '/api/workflows'
        if isinstance(op, ast.Eq):
            lstr, rstr = _const_str(left), _const_str(right)
            if lstr and lstr.startswith("/") and not isinstance(right, ast.Constant):
                return {"kind": "eq", "lit": lstr, "neg": False}
            if rstr and rstr.startswith("/") and not isinstance(left, ast.Constant):
                return {"kind": "eq", "lit": rstr, "neg": False}
            # segment token equality: parts[3] == 'task'
            if (i := _parts_idx(left)) is not None and (tok := _const_str(right)):
                return {"kind": "tok", "idx": i, "tok": tok, "neg": False}
            if (i := _parts_idx(right)) is not None and (tok := _const_str(left)):
                return {"kind": "tok", "idx": i, "tok": tok, "neg": False}
            # slice literal: parts[1:3] == ['api', 'workflow']
            if (
                isinstance(left, ast.Subscript)
                and isinstance(left.slice, ast.Slice)
                and _is_parts(left)
                and isinstance(right, ast.List)
            ):
                tokens = [_const_str(x) for x in right.elts]
                lo = _int_literal(left.slice.lower)
                if lo is not None and all(t is not None for t in tokens):
                    return {
                        "kind": "slice",
                        "start": lo,
                        "tokens": tokens,  # type: ignore[typeddict-item]
                        "neg": False,
                    }
        # length guard on segments: len(parts) >= 7
        if (
            isinstance(left, ast.Call)
            and isinstance(left.func, ast.Name)
            and left.func.id == "len"
            and len(left.args) == 1
            and isinstance(left.args[0], ast.Name)
            and left.args[0].id == "parts"
            and isinstance(right, ast.Constant)
            and isinstance(right.value, int)
        ):
            if isinstance(op, ast.Eq):
                cmp = "=="
            elif isinstance(op, ast.GtE):
                cmp = ">="
            elif isinstance(op, ast.Gt):
                cmp = ">"
            elif isinstance(op, ast.LtE):
                cmp = "<="
            elif isinstance(op, ast.Lt):
                cmp = "<"
            else:
                cmp = "?"
            return {"kind": "len", "cmp": cmp, "n": right.value, "neg": False}

    # path.startswith('/api/...') / path.endswith('/...')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr in {"startswith", "endswith"} and len(node.args) >= 1:
            lit = _const_str(node.args[0])
            if lit and lit.startswith("/"):
                return {
                    "kind": "prefix" if attr == "startswith" else "suffix",
                    "lit": lit,
                    "neg": False,
                }

    # `'/messages' in path` / `parts[6] in {'approve','reject'}`
    if isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
        left, right = node.left, node.comparators[0]
        neg = isinstance(node.ops[0], ast.NotIn)
        lit = _const_str(left)
        if lit and lit.startswith("/"):
            return {"kind": "substr", "lit": lit, "neg": neg}
        if (i := _parts_idx(left)) is not None and isinstance(right, (ast.Set, ast.Tuple, ast.List)):
            toks = [t for t in (_const_str(x) for x in right.elts) if t is not None]
            if toks:
                return {"kind": "tokset", "idx": i, "toks": toks, "neg": neg}
        if isinstance(left, ast.Name) and lit is None and _is_parts(right):
            # `parts[i] in {...}` appears the other way only for membership on a
            # subscript; handled above — nothing more to do here.
            pass
    return None


def _facts(cond: ast.AST) -> list[dict]:
    facts: list[dict] = []
    for part in _flatten_and(cond):
        fact = _fact(part)
        if fact:
            facts.append(fact)
    return facts


# --------------------------------------------------------------------------- #
# helper dispatch / param naming
# --------------------------------------------------------------------------- #


def _helper_param_names(cls: ast.ClassDef, helper: str) -> list[str]:
    """Positional parameter names of ``helper`` (self excluded)."""
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == helper:
            args = node.args.posonlyargs + node.args.args
            names = [a.arg for a in args]
            if names and names[0] == "self":
                names = names[1:]
            return names
    return []


def _collect_assigns(func: ast.AST, up_to_line: int) -> dict[str, ast.AST]:
    """Map ``name -> assigned value`` for assignments before ``up_to_line``."""
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and node.lineno is not None and node.lineno < up_to_line:
            _record_assign(bindings, node.targets, node.value)
    return bindings


def _record_assign(bindings: dict[str, ast.AST], targets: list[ast.AST], value: ast.AST) -> None:
    """Record ``name = value`` pairs, unwrapping tuple destructuring.

    Handles ``req_id = parts[3]`` and ``req_id, task_name = parts[3], parts[4]``.
    """
    if len(targets) == 1:
        t = targets[0]
        if isinstance(t, ast.Name):
            bindings[t.id] = value
            return
        if isinstance(t, ast.Tuple) and isinstance(value, (ast.Tuple, ast.List)):
            elts = value.elts
            if len(elts) == len(t.elts):
                for name, val in zip(t.elts, elts):
                    if isinstance(name, ast.Name):
                        bindings[name.id] = val
                return
    # rare multi-target (`a = b = parts[3]`) — bind every name to the value
    for t in targets:
        if isinstance(t, ast.Name):
            bindings[t.id] = value


def _binding_part_idx(value: ast.AST) -> int | None:
    """``name = parts[3]`` or ``name = parts[-2]`` -> index."""
    if _is_parts(value):
        return _int_literal(value.slice)
    return None


def _binding_end_offset(value: ast.AST) -> int | None:
    """``name = path.split('/')[-2]`` -> -2 (offset from the path end)."""
    if (
        isinstance(value, ast.Subscript)
        and isinstance(value.value, ast.Call)
        and isinstance(value.value.func, ast.Attribute)
        and value.value.func.attr == "split"
    ):
        return _int_literal(value.slice)
    return None


# --------------------------------------------------------------------------- #
# main synthesis
# --------------------------------------------------------------------------- #


def _detect_body_kind(value: ast.AST | None) -> str | None:
    """Classify an expression as reading a JSON payload or raw request bytes."""
    if value is None:
        return None
    if isinstance(value, ast.IfExp):
        return _detect_body_kind(value.body) or _detect_body_kind(value.orelse)
    if isinstance(value, ast.NamedExpr):
        return _detect_body_kind(value.value)
    if isinstance(value, ast.Call):
        func = value.func
        if isinstance(func, ast.Attribute):
            if func.attr == "loads" and isinstance(func.value, ast.Name) and func.value.id == "json":
                return "json"
            if func.attr == "read":  # self.rfile.read(length)
                return "bytes"
    return None


def _collect_body_kinds(do_method: ast.AST) -> dict[str, str]:
    """Map local names whose assignment reads an HTTP request body -> kind.

    Handles the two idioms seen in http.server handlers: ``body = json.loads(
    raw) if raw else {}`` (parsed JSON) and ``body = self.rfile.read(length) if
    length else b""`` (raw bytes). The body read usually sits at the top of the
    ``do_<VERB>`` method, sometimes wrapped in a ``try``.
    """
    kinds: dict[str, str] = {}

    def scan(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.Try, ast.If)):
                scan(stmt.body)
                if isinstance(stmt, ast.Try):
                    for handler in stmt.handlers:
                        scan(handler.body)
                scan(stmt.orelse)
                if isinstance(stmt, ast.Try):
                    scan(stmt.finalbody)
            elif isinstance(stmt, ast.Assign):
                kind = _detect_body_kind(stmt.value)
                if kind:
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            kinds.setdefault(target.id, kind)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                kind = _detect_body_kind(stmt.value)
                if kind:
                    kinds.setdefault(stmt.target.id, kind)

    scan(do_method.body)
    return kinds


def _endpoint_body_kind(args: list[ast.AST], body_kinds: dict[str, str]) -> str:
    """Kind of the request body this endpoint's helper consumes, if any."""
    for arg in args:
        if isinstance(arg, ast.Name) and arg.id in body_kinds:
            return body_kinds[arg.id]
    return ""


def synthesize_class_endpoints(source_code: str, class_name: str) -> list[EndpointSpec]:
    """Reconstruct endpoints for one handler class from its source text."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []
    cls = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None
    )
    if cls is None:
        return []

    specs: list[EndpointSpec] = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        verb = node.name[len("do_") :].upper() if node.name.startswith("do_") else None
        if verb not in HTTP_VERBS:
            continue
        _synthesize_do_method(cls, node, verb, specs)
    return specs


def _synthesize_do_method(cls: ast.ClassDef, do_method: ast.AST, verb: str, out: list[EndpointSpec]):
    leaves: list[tuple[list[ast.AST], ast.stmt]] = []
    _collect_leaves(do_method.body, [], leaves)
    body_kinds = _collect_body_kinds(do_method)

    for guards, leaf in leaves:
        helper, args = _leaf_helper(leaf)
        if helper is None:
            continue
        # send_json leaves count only when guarded by an exact path equality.
        if helper in _RESPONSE_HELPERS:
            if not any(f.get("kind") == "eq" for f in _facts_of(guards)):
                continue
            helper_name = f"{cls.name}.{do_method.name}"
        else:
            helper_name = helper

        facts = _facts_of(guards)
        path, params, note = _reconstruct(cls, do_method, verb, helper, args, facts, leaf)
        if not path:
            continue
        out.append(
            EndpointSpec(
                method=verb,
                path=path,
                handler=helper_name,
                line=leaf.lineno or do_method.lineno,
                params=params,
                class_name=cls.name,
                note=note,
                request_body_kind=_endpoint_body_kind(args, body_kinds),
            )
        )


def _facts_of(guards: list[ast.AST]) -> list[dict]:
    facts: list[dict] = []
    for cond in guards:
        facts.extend(_facts(cond))
    return facts


def _collect_leaves(body: list[ast.stmt], guards: list[ast.AST], out: list):
    """Collect ``(guard_tests, leaf_stmt)`` pairs for helper-call leaves."""
    for stmt in body:
        if isinstance(stmt, ast.If):
            # then-branch: guard = parent guards + this test
            _collect_leaves(stmt.body, guards + [stmt.test], out)
            if stmt.orelse:
                first = stmt.orelse[0]
                if len(stmt.orelse) == 1 and isinstance(first, ast.If):
                    # elif-chain: parent guard does not positively gate the elif
                    _collect_leaves(stmt.orelse, guards, out)
                else:
                    _collect_leaves(stmt.orelse, guards, out)
        elif isinstance(stmt, ast.Try):
            _collect_leaves(stmt.body, guards, out)
            for handler in stmt.handlers:
                _collect_leaves(handler.body, guards, out)
        elif isinstance(stmt, (ast.Return, ast.Expr)):
            helper, _ = _leaf_helper(stmt)
            if helper is not None:
                out.append((guards, stmt))


def _leaf_helper(stmt: ast.stmt) -> tuple[str | None, list[ast.AST]]:
    """``return self._list_workflows(...)`` / ``self._send_json(...)`` -> helper."""
    value = stmt.value if isinstance(stmt, (ast.Return, ast.Expr)) else None
    if not isinstance(value, ast.Call):
        return None, []
    func = value.func
    if not isinstance(func, ast.Attribute):
        return None, []
    if not (isinstance(func.value, ast.Name) and func.value.id == "self"):
        return None, []
    return func.attr, value.args


# --------------------------------------------------------------------------- #
# URL template reconstruction
# --------------------------------------------------------------------------- #


def _base_info(facts: list[dict]) -> tuple[str | None, int, list[str], int]:
    """Return (base_str, base_start_idx, base_tokens, base_end_idx).

    ``base_str`` is the literal static prefix ('/api/workflow'), ``base_start_idx``
    the first ``parts`` index the base occupies (default 1), ``base_tokens`` the
    path segment tokens of the base and ``base_end_idx`` the last occupied index.
    """
    base_str: str | None = None
    for f in facts:
        if f["kind"] == "prefix" and not f.get("neg"):
            base_str = f["lit"].rstrip("/")
            break
    base_tokens: list[str] = []
    base_start = 1
    for f in facts:
        if f["kind"] == "slice" and not f.get("neg"):
            base_tokens = [str(t) for t in f["tokens"]]
            base_start = int(f["start"])
            if base_str is None:  # a slice can also be the sole base
                base_str = "/" + "/".join(base_tokens)
            break
    if base_str is None:
        return None, 0, [], 0
    if not base_tokens:
        base_tokens = [seg for seg in base_str.split("/") if seg]
    end = base_start + len(base_tokens) - 1
    return base_str, base_start, base_tokens, end


def _reconstruct(cls, do_method, verb, helper, args, facts, leaf) -> tuple[str | None, list[str], str]:
    bindings = _collect_assigns(do_method, leaf.lineno or 0)
    helper_params = _helper_param_names(cls, helper)

    def _arg_part_index(arg: ast.AST) -> int | None:
        """Consumed ``parts[i]`` index (resolving negative via a length guard).

        A negative index is only pinned to an absolute one when an exact length
        guard is present. Otherwise the arg is an open-ended tail (e.g. the
        webapi fallback ``req_id = parts[-1]``) and stays unresolved so the
        caller routes it into the trailing-parameter reconstruction.
        """
        if (i := _parts_idx(arg)) is not None:
            if i >= 0:
                return i
            for f in facts:
                if f["kind"] == "len" and f["cmp"] == "==":
                    return int(f["n"]) + i
            return None
        if isinstance(arg, ast.Name):
            binding = bindings.get(arg.id)
            if binding is not None:
                i = _binding_part_idx(binding)
                if i is not None:
                    if i >= 0:
                        return i
                    for f in facts:
                        if f["kind"] == "len" and f["cmp"] == "==":
                            return int(f["n"]) + i
                    return None
        return None

    def _arg_param_name(arg: ast.AST, ordinal: int) -> str | None:
        """Best-effort param name for a dynamic path argument."""
        if isinstance(arg, ast.Name):
            if _binding_part_idx(bindings.get(arg.id)) is not None:
                return arg.id
        # bare parts[i] / ambiguous -> use helper signature position
        if ordinal < len(helper_params):
            return helper_params[ordinal]
        if (i := _parts_idx(arg)) is not None:
            return f"p{abs(i)}"
        return None

    # -- 1. exact equality: a fully static URL -------------------------------
    for f in facts:
        if f["kind"] == "eq" and not f.get("neg"):
            return f["lit"], [], "exact"

    base_str, base_start, _base_tokens, base_end = _base_info(facts)
    if base_str is None:
        return None, [], "no-prefix"

    # -- 2. absolute segment mapping ------------------------------------------
    literal_at: dict[int, str] = {}
    # seed the static base segments first (parts[base_start..base_end] are literal)
    for off, tok in enumerate(_base_tokens):
        literal_at[base_start + off] = tok
    for f in facts:
        if f["kind"] == "slice" and not f.get("neg"):
            for off, tok in enumerate(f["tokens"]):
                idx = int(f["start"]) + off
                if idx in literal_at and literal_at[idx] != tok:
                    return None, [], "conflict-base-token"
                literal_at[idx] = str(tok)
    for f in facts:
        if f["kind"] == "tok" and not f.get("neg"):
            idx = int(f["idx"])
            if idx in literal_at and literal_at[idx] != f["tok"]:
                return None, [], "conflict-token"  # contradictory guards
            literal_at[idx] = f["tok"]

    dynamic_idxs: list[int] = []
    param_at: dict[int, str] = {}
    unresolved: list[str] = []
    ordinal = 0
    for arg in args:
        if isinstance(arg, ast.Name) and arg.id in {"u", "body", "headers", "raw"}:
            continue
        idx = _arg_part_index(arg)
        if idx is None:
            # open-ended dynamic arg (negative tail, msg_idx neighbour, …)
            if isinstance(arg, ast.Name) and arg.id not in {"u", "body", "headers", "raw"}:
                unresolved.append(arg.id)
            ordinal += 1
            continue
        dynamic_idxs.append(idx)
        name = _arg_param_name(arg, ordinal)
        if name and idx not in param_at:
            param_at[idx] = name
        ordinal += 1

    # params may not sit on the static base (dead, self-contradictory branches
    # such as the unreachable `parts[3] == 'runs'` runs-family in webapi.py)
    for idx in dynamic_idxs:
        if base_start <= idx <= base_end:
            return None, [], "conflict-param-over-base"

    seg_max = max(
        base_end,
        *[i for i in dynamic_idxs if i > base_end],
        *[i for i in literal_at if i > base_end],
        0,
    )

    segments: list[str] = []
    ok = True
    for idx in range(base_start, seg_max + 1):
        if idx in literal_at:
            segments.append(literal_at[idx])
        elif idx in param_at:
            segments.append("{" + param_at[idx] + "}")
        elif idx > base_end:
            segments.append("{...}")
        else:
            ok = False
            break
    beyond_base = any(i > base_end for i in range(base_start, seg_max + 1))
    if ok and segments and beyond_base:
        path = "/" + "/".join(segments)
        params = [param_at[i] for i in sorted(param_at) if i > base_end]
        return path, params, "segments"

    # -- 3. prefix + suffix with a dynamic tail (POST /control etc.) -----------
    suffix = next((f["lit"] for f in facts if f["kind"] == "suffix" and not f.get("neg")), None)
    tail_names: list[str] = []
    if suffix:
        # parameter(s) that sit between prefix and the literal suffix
        for arg in args:
            if isinstance(arg, ast.Name) and arg.id in {"u", "body", "headers", "raw"}:
                continue
            name = None
            if isinstance(arg, ast.Name):
                binding = bindings.get(arg.id)
                if _binding_end_offset(binding) is not None:
                    name = arg.id
            if name is None and not isinstance(arg, ast.Name):
                name = _arg_param_name(arg, len(tail_names))
            elif name is None and isinstance(arg, ast.Name):
                # a bound name not anchored to the tail — still a dynamic arg
                if arg.id not in {"u", "body", "headers", "raw"}:
                    name = arg.id
            if name:
                tail_names.append(name)
        if tail_names:
            path = base_str + "".join("/{" + n + "}" for n in tail_names) + suffix
            return path, tail_names, "prefix+suffix"

    # -- 4. substring literal with dynamic neighbours (GET task/messages) ------
    substr = next(
        (f["lit"] for f in facts if f["kind"] == "substr" and not f.get("neg")), None
    )
    if substr:
        before, after = _neighbour_params(substr, bindings, args, helper_params)
        if before or after:
            if not substr.endswith("/"):
                # terminal token: /api/workflow/{req}/proposals
                path = base_str + "".join("/{" + n + "}" for n in before) + substr
                return path, before, "substr-terminal"
            # middle token: /api/workflow/{req}/messages/{task}
            path = (
                base_str
                + "".join("/{" + n + "}" for n in before)
                + substr.rstrip("/")
                + "".join("/{" + n + "}" for n in after)
            )
            return path, before + after, "substr-middle"

    # -- 5. open-ended dynamic tail (GET /api/workflow/{req_id}) ---------------
    # A branch of just ``base`` plus trailing path parameters and no further
    # literal anchors — e.g. the webapi fallback ``req_id = parts[-1]; return
    # self._get_workflow(req_id)``. Path args whose index could not be pinned
    # (negative-index ``parts`` bindings, name-only args) become trailing
    # ``{name}`` segments. Runs-family dead branches never reach here: they are
    # rejected earlier by the conflict rules in the absolute-segment pass.
    if base_str and unresolved:
        path = base_str + "".join("/{" + n + "}" for n in unresolved)
        return path, unresolved, "tail"

    # -- 6. coarse fallback: family prefix -------------------------------------
    if base_str:
        return base_str + "/…", [], "coarse"
    return None, [], "unresolved"


def _neighbour_params(substr: str, bindings, args, helper_params):
    """Names of dynamic segments around a middle substring like ``/messages/``."""
    before: list[str] = []
    after: list[str] = []
    ordinal = 0
    for arg in args:
        if isinstance(arg, ast.Name) and arg.id in {"u", "body", "headers", "raw"}:
            continue
        if isinstance(arg, ast.Name):
            binding = bindings.get(arg.id)
            if binding is not None and _is_parts(binding):
                expr = binding.slice
                # parts[msg_idx - 1] / parts[msg_idx + 1]
                if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Sub, ast.Add)):
                    if isinstance(expr.right, ast.Constant) and isinstance(expr.right.value, int):
                        off = expr.right.value if isinstance(expr.op, ast.Add) else -expr.right.value
                        (before if off < 0 else after).append((off, arg.id))
                        ordinal += 1
                        continue
        ordinal += 1
    before = [n for _, n in sorted(before)]
    after = [n for _, n in sorted(after)]
    return before, after
