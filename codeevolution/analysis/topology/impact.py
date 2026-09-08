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
        affected_messages = [
            edge
            for edge in getattr(topology, "message_edges", [])
            if edge.source_service == service or edge.target_service == service
        ]
        affected_resources = [
            edge
            for edge in getattr(topology, "resource_edges", [])
            if edge.source_service == service
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
                    "source_endpoint_method": getattr(edge, "source_endpoint_method", ""),
                    "source_endpoint_path": getattr(edge, "source_endpoint_path", ""),
                    "call_chain": getattr(edge, "call_chain", []),
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                }
                for edge in affected
            ],
            "affected_message_edges": [
                {
                    "from": f"{edge.source_service}::{edge.source_function}",
                    "to": f"{edge.target_service}::{edge.target_function}",
                    "broker_type": edge.broker_type,
                    "channel": edge.channel,
                    "source_endpoint_method": edge.source_endpoint_method,
                    "source_endpoint_path": edge.source_endpoint_path,
                    "call_chain": edge.call_chain,
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                }
                for edge in affected_messages
            ],
            "affected_resource_edges": [
                {
                    "from": f"{edge.source_service}::{edge.source_function}",
                    "to": edge.resource_id,
                    "resource_type": edge.resource_type,
                    "operation": edge.operation,
                    "resource_key": edge.resource_key,
                    "source_endpoint_method": edge.source_endpoint_method,
                    "source_endpoint_path": edge.source_endpoint_path,
                    "call_chain": edge.call_chain,
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                }
                for edge in affected_resources
            ],
        }
