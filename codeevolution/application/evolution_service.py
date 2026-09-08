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

    def query_events(self, **filters) -> dict:
        return self.store.query_events(**filters)

    def stats(self) -> dict:
        return self.store.get_stats()

    def get_feature(self, stable_id: str) -> dict | None:
        return self.store.get_feature(stable_id)

    def list_events(self, stable_id: str) -> list[dict]:
        return self.store.get_feature_timeline(stable_id)
