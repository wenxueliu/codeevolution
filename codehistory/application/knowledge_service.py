"""Knowledge extraction use cases."""


class KnowledgeService:
    def __init__(self, extractor, close=None):
        self.extractor = extractor
        self._close = close

    @classmethod
    def from_codegraph(cls, db_path: str):
        from ..infrastructure.codegraph_sqlite import SQLiteCodeGraphRepository
        from ..knowledge import KnowledgeExtractor

        repository = SQLiteCodeGraphRepository(db_path)
        return cls(KnowledgeExtractor(repository), repository.close)

    def report(self, include_llm: bool = False) -> dict:
        return self.extractor.extract_all(include_llm=include_llm)

    def section(self, name: str, **options):
        methods = {
            "api": self.extractor.extract_api_contract,
            "modules": self.extractor.extract_module_topology,
            "entities": self.extractor.extract_core_entities,
            "tests": self.extractor.extract_test_coverage_stats,
            "gaps": self.extractor.extract_test_gaps,
            "layers": self.extractor.extract_layer_violations,
            "config": self.extractor.extract_config_consumption,
            "deps": self.extractor.extract_external_dependencies,
            "auth": self.extractor.extract_authorization_model,
            "heatmap": self.extractor.extract_heat_map,
            "business": self.extractor.extract_business_descriptions,
            "rules": self.extractor.extract_business_rules_llm,
            "errors": self.extractor.extract_error_catalog,
            "states": self.extractor.extract_state_machines,
        }
        return methods[name](**options)

    def __getattr__(self, name):
        """Keep delivery migration source-compatible without exposing construction."""
        return getattr(self.extractor, name)

    def close(self):
        if self._close:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
