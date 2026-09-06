"""Compatibility facade for advanced topology analysis."""

from .analysis.topology.advanced_impl import (
    MQ_CONSUMER_PATTERNS,
    MQ_PRODUCER_PATTERNS,
    RPC_PATTERNS,
    AdvancedTopologyImplementation,
    CrossServiceEntities,
    EntityMapping,
    FlowDiagram,
    FlowStep,
)


class P2Analyzer:
    """Legacy API delegating advanced analysis to focused topology modules."""

    def __init__(self, repos: list[dict]):
        self._implementation = AdvancedTopologyImplementation(repos)

    def __getattr__(self, name):
        return getattr(self._implementation, name)

    _topics_match = staticmethod(AdvancedTopologyImplementation._topics_match)
    _extract_topic_from_decorator = staticmethod(
        AdvancedTopologyImplementation._extract_topic_from_decorator
    )
    _guess_topic = staticmethod(AdvancedTopologyImplementation._guess_topic)
    _entity_similarity = staticmethod(AdvancedTopologyImplementation._entity_similarity)
    _infer_relationship = staticmethod(AdvancedTopologyImplementation._infer_relationship)


__all__ = [
    "CrossServiceEntities",
    "EntityMapping",
    "FlowDiagram",
    "FlowStep",
    "MQ_CONSUMER_PATTERNS",
    "MQ_PRODUCER_PATTERNS",
    "P2Analyzer",
    "RPC_PATTERNS",
]
