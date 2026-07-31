class TopologyRenderer:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def topology(self, value) -> str:
        return self.analyzer.format_topology(value)

    def impact(self, value) -> str:
        return self.analyzer.format_impact(value)

    def trace(self, value) -> str:
        return self.analyzer.format_trace(value)
