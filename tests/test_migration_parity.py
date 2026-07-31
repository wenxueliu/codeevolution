"""Behavior parity gates between compatibility facades and application boundaries."""

from codehistory.analysis.topology.advanced_impl import AdvancedTopologyImplementation
from codehistory.analysis.topology.cross_repo_impl import CrossRepoImplementation
from codehistory.application.advanced_topology_service import AdvancedTopologyService
from codehistory.application.topology_service import TopologyService
from codehistory.cross_repo import CrossRepoAnalyzer
from codehistory.p2_advanced import P2Analyzer


def test_cross_repo_facade_service_and_implementation_deep_match():
    expected = CrossRepoImplementation([]).analyze()

    assert CrossRepoAnalyzer([]).analyze() == expected
    assert TopologyService.from_repositories([]).get_or_build() == expected


def test_advanced_facade_service_and_implementation_deep_match():
    expected_flow = AdvancedTopologyImplementation([]).trace_full_flow("gateway", "/orders")
    expected_entities = AdvancedTopologyImplementation([]).align_entities()
    service = AdvancedTopologyService.from_repositories([])

    assert P2Analyzer([]).trace_full_flow("gateway", "/orders") == expected_flow
    assert service.trace_flow("gateway", "/orders") == expected_flow
    assert P2Analyzer([]).align_entities() == expected_entities
    assert service.align_entities() == expected_entities
