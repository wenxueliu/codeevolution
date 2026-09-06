"""Natural-language assistant with a constrained, auditable query planner."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository

ALLOWED_OPERATIONS = {
    "codegraph.search_symbols",
    "codegraph.find_callers",
    "database.search_features",
    "database.list_events",
    "database.stats",
}


class ChatService:
    def __init__(self, audit_store, repository_resolver, store_resolver, llm_client=None):
        self.audit_store = audit_store
        self.repository_resolver = repository_resolver
        self.store_resolver = store_resolver
        self.llm_client = llm_client

    def ask(self, repository: str, question: str) -> dict:
        started = time.perf_counter()
        plan: list[dict] = []
        try:
            plan = self._plan(question)
            results = [self._execute(repository, operation) for operation in plan]
            result_count = sum(len(item.get("rows", [])) for item in results)
            answer = self._answer(question, results)
            audit_id = self.audit_store.record(
                repository=repository,
                question=question,
                plan=plan,
                status="success",
                result_count=result_count,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return {"answer": answer, "operations": results, "audit_id": audit_id}
        except Exception as error:
            self.audit_store.record(
                repository=repository,
                question=question,
                plan=plan,
                status="failed",
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(error),
            )
            raise

    def _plan(self, question: str) -> list[dict]:
        if self.llm_client:
            prompt = f"""Convert the user's repository question into JSON read operations.
Allowed operations: codegraph.search_symbols(keyword, limit),
codegraph.find_callers(symbol, limit), database.search_features(search, limit),
database.list_events(event_type, limit), database.stats().
Return only a JSON array with operation and args. Maximum 3 operations.
Never produce SQL. User question: {question}"""
            raw = self.llm_client.complete(prompt, max_tokens=500, temperature=0)
            parsed = self._parse_plan(raw or "")
            if parsed:
                return parsed
        return self._heuristic_plan(question)

    @staticmethod
    def _parse_plan(raw: str) -> list[dict]:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return []
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:3]:
            if not isinstance(item, dict) or item.get("operation") not in ALLOWED_OPERATIONS:
                continue
            result.append({"operation": item["operation"], "args": item.get("args") or {}})
        return result

    @staticmethod
    def _heuristic_plan(question: str) -> list[dict]:
        limit = 20
        identifiers = re.findall(r"`([^`]+)`|\b([A-Za-z_$][\w$.:/-]{2,})\b", question)
        keyword = next((left or right for left, right in reversed(identifiers)), "")
        if not keyword:
            keyword = re.sub(
                r"谁调用了?|调用方|查找|搜索|查询|功能|历史|事件|变更|统计|概览|多少|[?？,，。\s]",
                "",
                question,
            )
        keyword = keyword[:100]
        lowered = question.lower()
        if any(word in lowered for word in ("统计", "概览", "stats", "多少")):
            return [{"operation": "database.stats", "args": {}}]
        if any(word in lowered for word in ("事件", "变更", "event")):
            return [{"operation": "database.list_events", "args": {"event_type": "", "limit": limit}}]
        if any(word in lowered for word in ("功能", "feature", "历史")):
            return [{"operation": "database.search_features", "args": {"search": keyword, "limit": limit}}]
        if any(word in lowered for word in ("调用", "caller", "谁调用")):
            return [{"operation": "codegraph.find_callers", "args": {"symbol": keyword, "limit": limit}}]
        return [{"operation": "codegraph.search_symbols", "args": {"keyword": keyword, "limit": limit}}]

    def _execute(self, repository: str, item: dict) -> dict:
        operation = item["operation"]
        args = item.get("args") or {}
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"Unsupported assistant operation: {operation}")
        limit = max(1, min(int(args.get("limit", 20)), 100))
        if operation.startswith("codegraph."):
            rows = self._execute_codegraph(repository, operation, args, limit)
            source = "codegraph"
        else:
            rows = self._execute_database(repository, operation, args, limit)
            source = "evolution"
        return {"operation": operation, "args": args, "source": source, "rows": rows}

    def _execute_codegraph(self, repository: str, operation: str, args: dict, limit: int):
        members = self.repository_resolver(repository)
        rows = []
        for member in members:
            database = Path(member["path"]) / ".codegraph" / "codegraph.db"
            if not database.exists():
                continue
            with SQLiteCodeGraphRepository(str(database)) as graph:
                if operation == "codegraph.search_symbols":
                    keyword = str(args.get("keyword", ""))[:100]
                    found = graph.query(
                        """SELECT name, qualified_name, kind, file_path, start_line
                           FROM nodes WHERE name LIKE ? OR qualified_name LIKE ?
                           ORDER BY name LIMIT ?""",
                        [f"%{keyword}%", f"%{keyword}%", limit],
                    )
                else:
                    symbol = str(args.get("symbol", ""))[:100]
                    found = graph.query(
                        """SELECT DISTINCT caller.name, caller.qualified_name, caller.kind,
                                  caller.file_path, caller.start_line
                           FROM edges e JOIN nodes callee ON callee.id = e.target
                           JOIN nodes caller ON caller.id = e.source
                           WHERE e.kind = 'calls' AND (callee.name LIKE ? OR callee.qualified_name LIKE ?)
                           ORDER BY caller.name LIMIT ?""",
                        [f"%{symbol}%", f"%{symbol}%", limit],
                    )
            rows.extend({**row, "repository": member.get("name", repository)} for row in found)
        return rows[:limit]

    def _execute_database(self, repository: str, operation: str, args: dict, limit: int):
        store = self.store_resolver(repository)
        if operation == "database.stats":
            return [store.get_stats()]
        if operation == "database.search_features":
            search = str(args.get("search", ""))[:100]
            return store.query_features(status="all", search=search, limit=limit, offset=0)["features"]
        event_type = str(args.get("event_type", ""))[:40]
        return store.query_events(event_type=event_type, limit=limit, offset=0)["events"]

    @staticmethod
    def _answer(question: str, results: list[dict]) -> str:
        count = sum(len(item.get("rows", [])) for item in results)
        if not count:
            return f"没有找到与“{question}”匹配的数据。可以尝试使用更具体的类名、方法名或功能名。"
        operations = "、".join(item["operation"] for item in results)
        return f"已通过 {operations} 查询到 {count} 条结果，详情见下方操作结果。"
