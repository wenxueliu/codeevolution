"""Call-graph heat-map analysis."""

from collections.abc import Callable

HEATMAP_VERSION = 2


def _is_public(function) -> bool:
    """Language-neutral public check: explicit visibility, or no private naming."""
    return function.visibility not in ("private", "protected") and not function.name.startswith(
        ("_", "#")
    )


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
        # Resolve before ranking: call edges may point outside this repository,
        # and the graph may carry non-function nodes.  Test and non-public
        # functions are dropped with their edges so they do not inflate
        # production call counts.
        functions = {}
        for node_id in graph.nodes():
            function = self.source.get_function_by_id(node_id)
            if (
                function is None
                or function.kind not in ("function", "method")
                or function.is_test
                or not _is_public(function)
            ):
                continue
            functions[node_id] = function
        graph = graph.subgraph(functions)
        degrees = sorted(
            (
                (graph.in_degree(node_id), graph.out_degree(node_id), function)
                for node_id, function in functions.items()
            ),
            key=lambda item: -(item[0] + item[1]),
        )
        hot, warm = max(1, int(len(degrees) * 0.1)), max(2, int(len(degrees) * 0.6))
        results = []
        for index, (incoming, outgoing, function) in enumerate(degrees):
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
