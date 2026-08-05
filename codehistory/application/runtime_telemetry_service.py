"""Ingest runtime telemetry and associate it with static features/topology."""

from ..analysis.topology.matching import PathMatcher


class RuntimeTelemetryService:
    def __init__(self, collector, store, topology_service=None):
        self.collector = collector
        self.store = store
        self.topology_service = topology_service

    def ingest(self, payload: dict) -> dict:
        return self._persist(self.collector.collect(payload))

    def ingest_file(self, path) -> dict:
        return self._persist(self.collector.collect_file(path))

    def _persist(self, observations: list[dict]) -> dict:
        features = self.store.get_all_features()
        trace_features: dict[str, int] = {}
        persisted = []
        with self.store.transaction():
            for observation in sorted(observations, key=lambda item: item.get("kind") != "span"):
                feature_id = self._feature_id(observation, features, trace_features)
                observation_id = self.store.insert_runtime_observation(observation, feature_id)
                trace_id = observation.get("trace_id")
                if trace_id and feature_id is not None:
                    trace_features[trace_id] = feature_id
                persisted.append({**observation, "id": observation_id, "feature_id": feature_id})
        result = {
            "observations": persisted,
            "summary": {
                "total": len(persisted),
                "spans": sum(item["kind"] == "span" for item in persisted),
                "logs": sum(item["kind"] == "log" for item in persisted),
                "errors": sum(item["kind"] == "error" for item in persisted),
                "associated": sum(item["feature_id"] is not None for item in persisted),
            },
        }
        if self.topology_service is not None:
            result["topology_validation"] = self.topology_service.validate_runtime(
                [item for item in persisted if item["kind"] == "span"]
            )
        return result

    def _feature_id(self, observation, features, trace_features) -> int | None:
        attributes = observation.get("attributes") or {}
        stable_id = attributes.get("codehistory.feature.stable_id") or attributes.get(
            "feature.stable_id"
        )
        if stable_id:
            feature = self.store.get_feature(str(stable_id))
            if feature:
                return feature["id"]
        trace_id = observation.get("trace_id")
        if trace_id in trace_features:
            return trace_features[trace_id]
        method, path = observation.get("method", "").upper(), observation.get("path", "")
        if not path:
            return None
        for feature in features:
            parts = feature.get("entry_signature", "").split(maxsplit=1)
            if len(parts) == 2 and (not method or parts[0].upper() == method):
                if PathMatcher.matches(path, parts[1]):
                    return feature["id"]
        return None
