"""Call-graph heat-map analysis."""

from collections.abc import Callable


class HeatmapExtractor:
    def __init__(
        self, source, graph_provider=None, classify_layer: Callable[[str], str] | None = None
    ):
        self.source, self.graph_provider = source, graph_provider
        self.classify_layer = classify_layer or (lambda _path: "")

    def extract(self) -> list[dict]:
        if callable(self.source) and self.graph_provider is None:
            return self.source()
        graph = self.graph_provider()
        degrees = sorted(
            ((node, graph.in_degree(node), graph.out_degree(node)) for node in graph.nodes()),
            key=lambda item: -(item[1] + item[2]),
        )
        hot, warm = max(1, int(len(degrees) * 0.1)), max(2, int(len(degrees) * 0.6))
        results = []
        for index, (node_id, incoming, outgoing) in enumerate(degrees):
            function = self.source.get_function_by_id(node_id)
            if function is None:
                continue
            heat = "hot" if index < hot else "warm" if index < warm else "cold"
            results.append(
                {
                    "name": function.name,
                    "qualified_name": function.qualified_name,
                    "file_path": function.file_path,
                    "kind": function.kind,
                    "heat": heat,
                    "callers": incoming,
                    "callees": outgoing,
                    "total_degree": incoming + outgoing,
                    "layer": self.classify_layer(function.file_path),
                }
            )
        return results
