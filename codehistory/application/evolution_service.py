"""Shared evolution query use cases."""


class EvolutionQueryService:
    def __init__(self, store, close=None):
        self.store = store
        self._close = close

    @classmethod
    def from_database(cls, db_path: str):
        from ..store import EvolutionStore

        store = EvolutionStore(db_path)
        return cls(store, store.close)

    def close(self):
        if self._close:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def list_features(
        self, status: str = "all", search: str = "", limit: int = 100, offset: int = 0
    ) -> dict:
        if hasattr(self.store, "query_features"):
            return self.store.query_features(status, search, limit, offset)
        features = self.store.get_all_features()
        if status != "all":
            features = [item for item in features if item["status"] == status]
        if search:
            needle = search.lower()
            features = [
                item
                for item in features
                if needle in item["canonical_name"].lower()
                or needle in item["entry_signature"].lower()
            ]
        return {"total": len(features), "features": features[offset : offset + limit]}

    def list_features_at_commit(
        self,
        commit: str,
        status: str = "all",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        features = self.store.get_features_at_commit(commit)
        if status != "all":
            features = [item for item in features if item["status"] == status]
        if search:
            needle = search.lower()
            features = [
                item
                for item in features
                if needle in item["canonical_name"].lower()
                or needle in item["entry_signature"].lower()
            ]
        return {"total": len(features), "features": features[offset : offset + limit]}

    def explanation_context(self, stable_id: str) -> dict | None:
        """Load all data needed by an explanation without exposing store queries to HTTP."""
        feature = self.store.get_feature(stable_id)
        if not feature:
            return None
        snapshot = self.store.get_latest_snapshot(feature["id"])
        call_chain = snapshot.get("call_chain", []) if snapshot else []
        callee_names = {
            edge.get("to", "").replace("self.", "") for edge in call_chain if edge.get("to")
        }
        related = [
            item
            for item in self.store.get_all_features()
            if item["canonical_name"] in callee_names
        ]
        return {"feature": feature, "call_chain": call_chain, "related_features": related}

    def query_events(self, **filters) -> dict:
        return self.store.query_events(**filters)

    def event_stats(self) -> list[dict]:
        return self.store.event_stats()

    def stats(self) -> dict:
        return self.store.get_stats()

    def commits(self, limit=200) -> dict:
        commits = self.store.get_commits(limit)
        return {"total": len(commits), "commits": commits}

    def feature_detail(self, stable_id: str) -> dict | None:
        feature = self.store.get_feature(stable_id)
        if not feature:
            return None
        timeline = self.store.get_feature_timeline(stable_id)
        feature.update(timeline=timeline, event_count=len(timeline))
        snapshot = self.store.get_latest_snapshot(feature["id"])
        feature.update(snapshot or {"call_chain": []})
        if hasattr(self.store, "list_runtime_observations"):
            feature["runtime_observations"] = self.store.list_runtime_observations(
                feature_id=feature["id"]
            )
        return feature

    def capabilities(self) -> list[dict]:
        return self.store.get_capabilities()

    def get_feature(self, stable_id: str) -> dict | None:
        return self.store.get_feature(stable_id)

    def list_events(self, stable_id: str) -> list[dict]:
        return self.store.get_feature_timeline(stable_id)
