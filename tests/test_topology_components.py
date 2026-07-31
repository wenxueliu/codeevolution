from codehistory.analysis.topology.matching import EntitySimilarity, PathMatcher, TopicMatcher
from codehistory.cross_repo import CrossRepoAnalyzer


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
