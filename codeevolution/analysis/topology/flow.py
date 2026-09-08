"""Cycle-safe HTTP topology flow tracing."""


class FlowTracer:
    def __init__(self, analyzer=None):
        self.analyzer = analyzer

    def trace(self, topology, service: str, path: str = "", max_depth: int = 5):
        if not hasattr(topology, "cross_edges") and self.analyzer is not None:
            return self.analyzer.trace_flow(topology, service, path, max_depth)
        visited, chain = set(), []

        def follow(current, depth, entry_handler=""):
            if depth > max_depth:
                return
            for edge in topology.cross_edges:
                if edge.source_service != current:
                    continue
                source_path = getattr(edge, "source_endpoint_path", "")
                source_handler = getattr(edge, "source_endpoint_handler", "")
                if depth == 0 and path and source_path and source_path != path:
                    continue
                if depth > 0 and entry_handler and source_handler and source_handler != entry_handler:
                    continue
                key = (
                    edge.source_service,
                    source_path,
                    edge.source_function,
                    edge.target_service,
                    edge.url_pattern,
                )
                if key in visited:
                    continue
                visited.add(key)
                chain.append(
                    {
                        "depth": depth,
                        "from_service": edge.source_service,
                        "from_function": edge.source_function,
                        "to_service": edge.target_service,
                        "to_function": edge.target_function,
                        "method": edge.http_method,
                        "url": edge.url_pattern,
                        "source_endpoint_method": getattr(edge, "source_endpoint_method", ""),
                        "source_endpoint_path": source_path,
                        "call_chain": getattr(edge, "call_chain", []),
                    }
                )
                follow(edge.target_service, depth + 1, edge.target_function)

        follow(service, 0)
        return chain
