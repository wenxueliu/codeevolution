"""Per-node business-rule explanations for the call-chain tree.

While ``CallTreeService`` answers "which nodes does this node call?", this module
answers "what business logic does *this one node* perform?" for the right-hand
rail of the call-chain tree. It resolves a single node to its source snippet,
produces a default LLM prompt around that snippet, and leaves persistence to
``NodeRuleStore`` (upsert-on-generate, so a stored explanation is overwritten
when regenerated).

Two node kinds are explainable:

  * ``func``  — an intra-repo function node; identified by ``node_id`` (+ member)
  * ``cross`` — a matched downstream handler; identified by its qualified name
                within the target logical service

The default prompt asks for the same narrative JSON shape as the endpoint-level
business rules (``business_purpose_*`` / ``business_flow_*`` / ``business_rules``
/ ``side_effects``), so the frontend renders both with one panel.
"""

from __future__ import annotations

from pathlib import Path

from ...infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
from ...infrastructure.source_filesystem import FileSystemSourceProvider
from ...registry import get_repo, repository_members

# Keep the source snippet handed to the LLM bounded.
SNIPPET_MAX_LINES = 120

DEFAULT_NODE_RULE_PROMPT = """You are a senior software architect explaining backend business logic to product managers and developers.

The function below sits inside an API endpoint call chain of service {service} (member: {member}). {cross_note}Analyze the function's source and explain what business logic it performs:

1. **Business purpose**: What business function does this code serve? (1-2 sentences in English and Chinese)
2. **Business flow**: Step by step, in business terms, what the code does (not code).
3. **Key business rules**: What business constraints, validations, guard clauses, or decisions are embedded in the code?
4. **Side effects**: What databases, external systems, or services does it touch or change?

Function: {qualified_name}
File: {file}:{line}

Source code:
```
{snippet}
```

Output JSON:
{{
  "business_purpose_en": "...",
  "business_purpose_zh": "...",
  "business_flow_en": ["Step 1: ...", "Step 2: ...", ...],
  "business_flow_zh": ["步骤1: ...", "步骤2: ...", ...],
  "business_rules": ["Rule 1: ...", ...],
  "side_effects": ["Effect 1: ...", ...]
}}

JSON:"""


def _member_key(member: dict) -> str:
    return str(member.get("name") or Path(member["path"]).name)


def _codegraph_db(member: dict) -> Path:
    return Path(member["path"]) / ".codegraph" / "codegraph.db"


def _pick_member(members: list[dict], member: str | None) -> dict | None:
    if member:
        return next((m for m in members if _member_key(m) == member), None)
    return members[0] if len(members) == 1 else None


class NodeRuleService:
    """Resolve one call-chain node to its source context for an LLM explanation."""

    # ── resolution ─────────────────────────────────────────────────────

    def resolve(
        self,
        service: str,
        member: str | None = None,
        *,
        node_type: str = "func",
        node_id: str | None = None,
        handler: str | None = None,
    ) -> tuple[dict | None, str | None]:
        """Resolve ``node_type``+descriptor to a source context dict.

        Returns ``(ctx, None)`` on success or ``(None, error_message)`` when the
        node cannot be resolved. ``ctx`` holds::

            {service, member, node_type, node_key, qualified_name, name,
             file, line, snippet, node}
        """
        entry = get_repo(service)
        if not entry:
            return None, "服务未注册"
        members = repository_members(entry)
        if not members:
            return None, "服务没有可用的物理成员"

        if node_type == "func":
            member_dict = _pick_member(members, member)
            if member_dict is None:
                return None, "未指定物理成员（或成员不存在）"
            db = _codegraph_db(member_dict)
            if not db.exists():
                return None, f"成员 {_member_key(member_dict)} 缺少 CodeGraph 数据库，请先 codegraph init"
            with SQLiteCodeGraphRepository(str(db)) as reader:
                fn = reader.get_function_by_id(node_id or "")
                if fn is None:
                    return None, "未找到该函数节点"
            member_key = _member_key(member_dict)
            qname = fn.qualified_name or fn.name or (node_id or "")
            node_key = f"{member_key}::{qname}"
            ctx = {
                "service": service,
                "member": member_key,
                "repo_root": str(Path(member_dict["path"])),
                "node_type": "func",
                "node_key": node_key,
                "qualified_name": qname,
                "name": fn.name or "",
                "file": fn.file_path or "",
                "line": fn.start_line,
                "snippet": self._snippet(Path(member_dict["path"]), fn.file_path, fn.start_line, fn.end_line),
            }
            return ctx, None

        if node_type == "cross":
            if not handler:
                return None, "跨服务节点需要 handler 参数"
            for m in members:
                db = _codegraph_db(m)
                if not db.exists():
                    continue
                with SQLiteCodeGraphRepository(str(db)) as reader:
                    node = reader.function_node(handler)
                    if node is not None:
                        break
            else:
                node = None
            if node is None:
                return None, "未找到该跨服务处理函数"
            member_key = _member_key(m)
            ctx = {
                "service": service,
                "member": member_key,
                "repo_root": str(Path(m["path"])),
                "node_type": "cross",
                "node_key": f"{service}::{handler}",
                "qualified_name": handler,
                "name": node.get("name") or handler,
                "file": node.get("file_path") or "",
                "line": node.get("start_line") or 0,
                "snippet": self._snippet(Path(m["path"]), node.get("file_path"), node.get("start_line"), node.get("end_line")),
            }
            return ctx, None

        return None, f"不支持的节点类型: {node_type}"

    def default_prompt(self, ctx: dict) -> str:
        """Build the default prompt for a resolved context (no user override)."""
        cross_note = (
            "It is the handler of a downstream service endpoint reached by a cross-service HTTP call. "
            if ctx.get("node_type") == "cross"
            else ""
        )
        return DEFAULT_NODE_RULE_PROMPT.format(
            service=ctx.get("service", ""),
            member=ctx.get("member", ""),
            cross_note=cross_note,
            qualified_name=ctx.get("qualified_name", "") or ctx.get("name", ""),
            file=ctx.get("file", ""),
            line=ctx.get("line", 0),
            snippet=ctx.get("snippet") or "(当前节点源码不可读取)",
        )

    @staticmethod
    def _snippet(root: Path, file_path: str, start: int | None, end: int | None) -> str:
        if not file_path or not start:
            return ""
        cap = (start or 1) + SNIPPET_MAX_LINES - 1
        end = min(end or cap, cap) if end else cap
        try:
            return FileSystemSourceProvider(root).snippet(file_path, start, end) or ""
        except (OSError, UnicodeError):
            return ""


class SnapshotNodeRuleService(NodeRuleService):
    """Resolve rule input solely from a frozen ``RepositorySnapshotHandle``.

    Kept separate from the legacy service while delivery callers migrate, so
    snapshot code cannot accidentally use its registry/path helpers.
    """

    def resolve(self, handle, node_id: str) -> dict | None:  # type: ignore[override]
        fn = handle.graph.get_function_by_id(node_id)
        if fn is None:
            return None
        start = fn.start_line or 1
        end = min(fn.end_line or start + SNIPPET_MAX_LINES - 1, start + SNIPPET_MAX_LINES - 1)
        return {
            "repository_snapshot_id": handle.snapshot.id,
            "member_id": handle.snapshot.member_id,
            "service": handle.snapshot.member_id,
            "member": handle.snapshot.member_id,
            "node_type": "func",
            "node_key": f"{handle.snapshot.id}::{fn.node_id}",
            "node_id": fn.node_id,
            "qualified_name": fn.qualified_name or fn.name or node_id,
            "name": fn.name or "",
            "file": fn.file_path or "",
            "line": start,
            "snippet": handle.sources.snippet(fn.file_path or "", start, end) or "",
        }
