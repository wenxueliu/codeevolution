"""Cross-service change impact analysis."""


class ImpactAnalyzer:
    def __init__(self, analyzer=None):
        self.analyzer = analyzer

    def analyze(self, topology, service: str):
        if not hasattr(topology, "dependency_graph") and self.analyzer is not None:
            return self.analyzer.impact_analysis(topology, service)
        downstream = topology.dependency_graph.get(service, [])
        upstream = [
            name
            for name, dependencies in topology.dependency_graph.items()
            if service in dependencies
        ]
        affected = [
            edge
            for edge in topology.cross_edges
            if edge.source_service == service or edge.target_service == service
        ]
        return {
            "service": service,
            "upstream_impact": upstream,
            "downstream_impact": downstream,
            "affected_cross_edges": [
                {
                    "from": f"{edge.source_service}::{edge.source_function}",
                    "to": f"{edge.target_service}::{edge.target_function}",
                    "method": edge.http_method,
                    "url": edge.url_pattern,
                }
                for edge in affected
            ],
        }
