from types import SimpleNamespace

from codehistory.analysis.topology.builder import TopologyBuilder
from codehistory.analysis.topology.flow import FlowTracer
from codehistory.analysis.topology.impact import ImpactAnalyzer
from codehistory.analysis.topology.matching import EntitySimilarity, PathMatcher, TopicMatcher
from codehistory.cross_repo import CrossRepoAnalyzer
from codehistory.delivery.renderers import TopologyRenderer


def test_path_matcher_and_legacy_facade_agree():
    cases = [
        ("/api/users/123", "/api/users/:id", True),
        ("/api/users/123/posts", "/api/users/{id}/posts", True),
        ("/api/users/123", "/api/orders/:id", False),
    ]
    for actual, template, expected in cases:
        assert PathMatcher.matches(actual, template) is expected
        assert CrossRepoAnalyzer._paths_match(actual, template) is expected


def test_topic_and_entity_matchers_are_pure():
    assert TopicMatcher.matches("order-created", "order_created")
    assert not TopicMatcher.matches("orders", "payments")
    assert EntitySimilarity.score("UserService", "UserSvc") == 1.0


def test_topology_components_and_renderer_delegate_to_explicit_ports():
    analyzer = SimpleNamespace(
        analyze=lambda: "topology",
        impact_analysis=lambda topology, service: (topology, service),
        trace_flow=lambda topology, service, path, depth: (topology, service, path, depth),
        format_topology=lambda value: f"topology:{value}",
        format_impact=lambda value: f"impact:{value}",
        format_trace=lambda value: f"trace:{value}",
    )
    assert TopologyBuilder(analyzer).build() == "topology"
    assert ImpactAnalyzer(analyzer).analyze("topology", "orders") == ("topology", "orders")
    assert FlowTracer(analyzer).trace("topology", "orders") == ("topology", "orders", "", 5)
    renderer = TopologyRenderer(analyzer)
    assert renderer.topology("x") == "topology:x"
    assert renderer.impact("x") == "impact:x"
    assert renderer.trace("x") == "trace:x"
