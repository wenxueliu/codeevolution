class FlowTracer:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def trace(self, topology, service: str, path: str = "", max_depth: int = 5):
        return self.analyzer.trace_flow(topology, service, path, max_depth)

