"""Shared evolution query use cases."""


class EvolutionQueryService:
    def __init__(self, store):
        self.store = store

    def list_features(
        self, status: str = "all", search: str = "", limit: int = 100, offset: int = 0
    ) -> dict:
        features = self.store.get_all_features()
        if status != "all":
            features = [feature for feature in features if feature["status"] == status]
        if search:
            needle = search.lower()
            features = [
                feature
                for feature in features
                if needle in feature["canonical_name"].lower()
                or needle in feature["entry_signature"].lower()
            ]
        return {"total": len(features), "features": features[offset : offset + limit]}

    def get_feature(self, stable_id: str) -> dict | None:
        return self.store.get_feature(stable_id)

    def list_events(self, stable_id: str) -> list[dict]:
        return self.store.get_feature_timeline(stable_id)
