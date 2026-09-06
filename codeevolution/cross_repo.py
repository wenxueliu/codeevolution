"""Compatibility facade for multi-repository topology analysis."""

from .analysis.topology.cross_repo_impl import (
    HTTP_CLIENT_CALLERS,
    CrossRepoImplementation,
    CrossServiceEdge,
    OutboundCall,
    ServiceNode,
    UnifiedTopology,
)


class CrossRepoAnalyzer:
    """Legacy API delegating topology analysis to the analysis package."""

    def __init__(self, repos: list[dict], rules=None):
        self._implementation = CrossRepoImplementation(repos, rules)

    def __getattr__(self, name):
        return getattr(self._implementation, name)

    _paths_match = staticmethod(CrossRepoImplementation._paths_match)
    _extract_path = staticmethod(CrossRepoImplementation._extract_path)
    _extract_host = staticmethod(CrossRepoImplementation._extract_host)


__all__ = [
    "CrossRepoAnalyzer",
    "CrossServiceEdge",
    "HTTP_CLIENT_CALLERS",
    "OutboundCall",
    "ServiceNode",
    "UnifiedTopology",
]
