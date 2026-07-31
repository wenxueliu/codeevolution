class TopologyBuilder:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def build(self):
        return self.analyzer.analyze()
