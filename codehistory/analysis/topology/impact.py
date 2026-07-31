class ImpactAnalyzer:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def analyze(self, topology, service: str):
        return self.analyzer.impact_analysis(topology, service)
