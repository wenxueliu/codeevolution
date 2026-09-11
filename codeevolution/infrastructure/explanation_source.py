"""Freeze one API endpoint's reachable CodeGraph nodes for explanation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from ..registry import get_repo, repository_members
from .codegraph_sqlite import SQLiteCodeGraphRepository
from .source_filesystem import FileSystemSourceProvider

MAX_EXPLANATION_NODES = 500
MAX_CALLEES_PER_NODE = 200


def api_key(method: str, path: str, handler: str) -> str:
    return "|".join((method.upper(), path, handler))


class RepositoryExplanationSource:
    """Load a bounded, auditable intra-repository call graph."""

    def load(self, spec: dict) -> dict:
        entry = get_repo(spec["repo"])
        if not entry:
            raise ValueError(f"Repo '{spec['repo']}' not found")
        members = repository_members(entry)
        member = next(
            (
                item
                for item in members
                if not spec.get("member")
                or (item.get("name") or Path(item["path"]).name) == spec["member"]
            ),
            None,
        )
        if member is None:
            raise ValueError(f"Repository member '{spec.get('member', '')}' not found")
        member_name = member.get("name") or Path(member["path"]).name
        root = Path(member["path"])
        database = root / ".codegraph" / "codegraph.db"
        if not database.exists():
            raise ValueError(f"CodeGraph database not found for '{member_name}'")

        source = FileSystemSourceProvider(root)
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        unresolved = 0
        truncated = False
        with SQLiteCodeGraphRepository(str(database)) as reader:
            root_node = None
            if spec.get("file") and spec.get("line"):
                root_node = reader.handler_for_route(spec["file"], int(spec["line"]))
            if root_node is None and spec.get("handler"):
                root_node = reader.function_node(spec["handler"])
            if root_node is None:
                raise ValueError("API handler could not be resolved in CodeGraph")

            queue = [str(root_node["id"])]
            visited: set[str] = set()
            while queue:
                node_id = queue.pop(0)
                if node_id in visited:
                    continue
                if len(visited) >= MAX_EXPLANATION_NODES:
                    truncated = True
                    break
                visited.add(node_id)
                fn = reader.get_function_by_id(node_id)
                if fn is None or fn.kind not in {"function", "method"}:
                    unresolved += 1
                    continue
                text = source.snippet(fn.file_path, fn.start_line, fn.end_line) or ""
                node_key = f"{member_name}::{fn.node_id}"
                nodes[node_id] = {
                    "id": node_id,
                    "node_key": node_key,
                    "name": fn.name,
                    "qualified_name": fn.qualified_name,
                    "signature": fn.signature or "",
                    "file": fn.file_path,
                    "line_start": fn.start_line,
                    "line_end": fn.end_line,
                    "language": fn.language,
                    "source": text,
                    "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
                callees = reader.get_callee_rows(node_id, MAX_CALLEES_PER_NODE + 1)
                if len(callees) > MAX_CALLEES_PER_NODE:
                    truncated = True
                for callee in callees[:MAX_CALLEES_PER_NODE]:
                    target_id = str(callee.get("id") or "")
                    target = reader.get_function_by_id(target_id) if target_id else None
                    if target is None or target.kind not in {"function", "method"}:
                        unresolved += 1
                        continue
                    edges.append(
                        {
                            "source": node_id,
                            "target": target_id,
                            "call_line": callee.get("call_line"),
                            "call_site": {
                                "file": fn.file_path,
                                "line": callee.get("call_line"),
                            },
                        }
                    )
                    if target_id not in visited:
                        queue.append(target_id)

        # Edges to nodes removed by the global cap are explicitly dropped while
        # the snapshot remains marked truncated.
        edges = [edge for edge in edges if edge["source"] in nodes and edge["target"] in nodes]
        source_digest = self._digest(
            [(key, node["source_hash"]) for key, node in sorted(nodes.items())]
        )
        graph_digest = self._digest(
            [(edge["source"], edge["target"], edge.get("call_line")) for edge in edges]
        )
        return {
            "repo": spec["repo"],
            "member": member_name,
            "api_key": api_key(spec["method"], spec["path"], spec["handler"]),
            "entry_id": str(root_node["id"]),
            "entry_node_key": nodes[str(root_node["id"])]["node_key"],
            "nodes": nodes,
            "edges": edges,
            "source_revision": self._revision(root),
            "source_digest": source_digest,
            "graph_digest": graph_digest,
            "truncated": truncated,
            "unresolved_external_nodes": unresolved,
        }

    @staticmethod
    def _digest(value) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _revision(root: Path) -> str:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
            return f"{commit}:dirty" if dirty else commit
        except (OSError, subprocess.CalledProcessError):
            return "working-tree"


class SnapshotExplanationSource:
    """Build explanation input from immutable evidence, never a checkout.

    ``spec`` must carry ``repository_snapshot_id`` and a handler (or frozen
    file/line).  It intentionally mirrors the legacy source payload so the
    generation pipeline can migrate without trusting browser-provided graph
    data.
    """

    def __init__(self, snapshot_queries):
        self.snapshot_queries = snapshot_queries

    def load(self, spec: dict) -> dict:
        snapshot_id = str(spec.get("repository_snapshot_id") or "")
        if not snapshot_id:
            raise ValueError("repository_snapshot_id is required")
        with self.snapshot_queries.open(snapshot_id) as handle:
            reader, source = handle.graph, handle.sources
            root_node = None
            if spec.get("file") and spec.get("line"):
                root_node = reader.handler_for_route(spec["file"], int(spec["line"]))
            if root_node is None and spec.get("handler"):
                root_node = reader.function_node(spec["handler"])
            if root_node is None:
                raise ValueError("API handler could not be resolved in snapshot")
            nodes: dict[str, dict] = {}
            edges: list[dict] = []
            queue, visited, unresolved, truncated = [str(root_node["id"])], set(), 0, False
            while queue:
                node_id = queue.pop(0)
                if node_id in visited:
                    continue
                if len(visited) >= MAX_EXPLANATION_NODES:
                    truncated = True
                    break
                visited.add(node_id)
                fn = reader.get_function_by_id(node_id)
                if fn is None or fn.kind not in {"function", "method"}:
                    unresolved += 1
                    continue
                text = source.snippet(fn.file_path, fn.start_line, fn.end_line) or ""
                nodes[node_id] = {
                    "id": node_id, "node_key": f"{snapshot_id}::{fn.node_id}", "name": fn.name,
                    "qualified_name": fn.qualified_name, "signature": fn.signature or "",
                    "file": fn.file_path, "line_start": fn.start_line, "line_end": fn.end_line,
                    "language": fn.language, "source": text,
                    "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
                callees = reader.get_callee_rows(node_id, MAX_CALLEES_PER_NODE + 1)
                if len(callees) > MAX_CALLEES_PER_NODE:
                    truncated = True
                for callee in callees[:MAX_CALLEES_PER_NODE]:
                    target_id = str(callee.get("id") or "")
                    target = reader.get_function_by_id(target_id) if target_id else None
                    if target is None or target.kind not in {"function", "method"}:
                        unresolved += 1
                        continue
                    edges.append({"source": node_id, "target": target_id, "call_line": callee.get("call_line"),
                                  "call_site": {"file": fn.file_path, "line": callee.get("call_line")}})
                    if target_id not in visited:
                        queue.append(target_id)
            edges = [edge for edge in edges if edge["source"] in nodes and edge["target"] in nodes]
            source_digest = RepositoryExplanationSource._digest(
                [(key, node["source_hash"]) for key, node in sorted(nodes.items())]
            )
            graph_digest = RepositoryExplanationSource._digest(
                [(edge["source"], edge["target"], edge.get("call_line")) for edge in edges]
            )
            return {"repo": handle.snapshot.member_id, "member": handle.snapshot.member_id,
                    "repository_snapshot_id": snapshot_id,
                    "api_key": api_key(spec["method"], spec["path"], spec["handler"]),
                    "entry_id": str(root_node["id"]), "entry_node_key": nodes[str(root_node["id"])]["node_key"],
                    "nodes": nodes, "edges": edges, "source_revision": snapshot_id,
                    "source_digest": source_digest, "graph_digest": graph_digest,
                    "truncated": truncated, "unresolved_external_nodes": unresolved}
