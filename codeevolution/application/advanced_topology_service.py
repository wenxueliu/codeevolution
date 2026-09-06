"""Advanced cross-service topology use cases."""


class AdvancedTopologyService:
    """Application boundary for multi-channel flow and entity analysis."""

    def __init__(self, analyzer):
        self.analyzer = analyzer

    @classmethod
    def from_repositories(cls, repositories: list[dict]):
        from ..analysis.topology.advanced_impl import AdvancedTopologyImplementation

        return cls(AdvancedTopologyImplementation(repositories))

    def trace_flow(self, service: str, path: str = ""):
        return self.analyzer.trace_full_flow(service, path)

    def align_entities(self, use_llm: bool = False):
        return self.analyzer.align_entities(use_llm=use_llm)
