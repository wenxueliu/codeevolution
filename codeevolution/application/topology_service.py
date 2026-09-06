"""Topology orchestration with one build per use-case invocation."""


class TopologyService:
    def __init__(self, builder, cache=None):
        self.builder = builder
        self.cache = cache

    @classmethod
    def from_repositories(cls, repositories: list[dict], cache=None):
        """Compose the production topology stack outside delivery adapters."""
        from ..analysis.topology.builder import TopologyBuilder
        from ..analysis.topology.cross_repo_impl import CrossRepoImplementation

        analyzer = CrossRepoImplementation(repositories)
        return cls(TopologyBuilder(analyzer), cache)

    @property
    def analyzer(self):
        """Expose formatting compatibility until renderers accept domain DTOs directly."""
        return self.builder.analyzer

    def get_or_build(self, force: bool = False):
        if self.cache is not None and not force:
            cached = self.cache.load()
            if cached is not None:
                return cached
        topology = self.builder.build()
        if self.cache is not None:
            self.cache.save(topology)
        return topology

    def impact(self, analyzer, service: str, force: bool = False):
        topology = self.get_or_build(force=force)
        return analyzer.analyze(topology, service)

    def trace(self, tracer, service: str, path: str = "", force: bool = False):
        topology = self.get_or_build(force=force)
        return tracer.trace(topology, service, path)

    def validate_runtime(self, spans: list[dict], force: bool = False):
        from ..analysis.topology.runtime_validation import RuntimeTopologyValidator

        return RuntimeTopologyValidator().validate(self.get_or_build(force=force), spans)
