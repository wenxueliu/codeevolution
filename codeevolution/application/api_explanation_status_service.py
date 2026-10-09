"""Read-only status projection for endpoint-scoped API explanations.

For every endpoint of a frozen repository snapshot, decide whether its current
explanation is fresh (``explained`` / 已解释), stale because the reachable code
changed since it was generated (``stale`` / 待刷新), or never generated
(``pending`` / 待解释).  A generation still in flight surfaces as ``running``.
"""

from __future__ import annotations

from typing import Any, Iterable

PENDING = "pending"
RUNNING = "running"
EXPLAINED = "explained"
STALE = "stale"

# Explanation snapshot statuses with no published result yet.
_IN_FLIGHT_STATUSES = frozenset({"pending", "running", "validating"})


def _api_key(method: str, path: str, handler: str) -> str:
    return "|".join((method.upper(), path, handler))


class ApiExplanationStatusService:
    """Project explanation freshness for a whole endpoint list in one pass."""

    def __init__(self, store):
        self.store = store

    def statuses(
        self,
        repository_snapshot_id: str,
        endpoints: Iterable[dict[str, Any]],
        *,
        member_id: str,
        source_loader,
    ) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        candidates: list[Any] = []
        specs: list[dict[str, Any] | None] = []
        for raw in endpoints:
            if not isinstance(raw, dict):
                continue
            record, candidate, spec = self._classify(repository_snapshot_id, raw, member_id)
            records.append(record)
            candidates.append(candidate)
            specs.append(spec)

        to_validate = [index for index, spec in enumerate(specs) if spec is not None]
        if to_validate:
            resolved = self._resolve(
                source_loader, repository_snapshot_id, [specs[index] for index in to_validate]
            )
            for index, current in zip(to_validate, resolved):
                records[index]["status"] = self._freshness(current, candidates[index])

        summary = {
            EXPLAINED: sum(1 for item in records if item["status"] == EXPLAINED),
            STALE: sum(1 for item in records if item["status"] == STALE),
            PENDING: sum(1 for item in records if item["status"] == PENDING),
            RUNNING: sum(1 for item in records if item["status"] == RUNNING),
        }
        return {
            "repository_snapshot_id": repository_snapshot_id,
            "summary": summary,
            "endpoints": records,
        }

    def _classify(self, repository_snapshot_id, endpoint, member_id):
        """Return (record, candidate_snapshot, spec_to_validate_or_None)."""
        method = str(endpoint.get("method") or "")
        path = str(endpoint.get("path") or "")
        handler = str(endpoint.get("handler") or "")
        key = _api_key(method, path, handler)
        base = {"api_key": key, "method": method, "path": path, "handler": handler,
                "snapshot_id": "", "source_revision": "", "created_at": None}

        candidate = (
            self.store.get_current_for_repository_snapshot(repository_snapshot_id, key)
            or self.store.get_current(member_id, member_id, key)
        )
        if candidate is None:
            return {**base, "status": PENDING}, None, None

        meta = {"snapshot_id": candidate.id, "source_revision": candidate.source_revision,
                "created_at": candidate.created_at}
        if candidate.status in _IN_FLIGHT_STATUSES:
            return {**base, **meta, "status": RUNNING}, candidate, None
        if candidate.repository_snapshot_id == repository_snapshot_id:
            # Same frozen evidence the explanation was generated from: the
            # fingerprints are identical by construction, so skip the graph walk.
            return {**base, **meta, "status": EXPLAINED}, candidate, None

        spec = {"repository_snapshot_id": repository_snapshot_id, "method": method,
                "path": path, "handler": handler,
                "file": endpoint.get("file") or "", "line": endpoint.get("line")}
        return {**base, **meta, "status": STALE}, candidate, spec

    @staticmethod
    def _freshness(current, candidate) -> str:
        if (
            current
            and current.get("source_digest") == candidate.source_digest
            and current.get("graph_digest") == candidate.graph_digest
        ):
            return EXPLAINED
        return STALE

    @staticmethod
    def _resolve(source_loader, repository_snapshot_id, specs):
        """Resolve specs to frozen payloads, preferring a single-open batch."""
        if not specs:
            return []
        if hasattr(source_loader, "load_many"):
            try:
                resolved = list(source_loader.load_many(repository_snapshot_id, specs))
                if len(resolved) == len(specs):
                    return resolved
            except Exception:
                pass
        results = []
        for spec in specs:
            try:
                results.append(source_loader.load(spec))
            except Exception:
                results.append(None)
        return results
