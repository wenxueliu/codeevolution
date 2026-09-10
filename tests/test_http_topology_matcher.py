from codeevolution.analysis.topology.http_matcher import (
    HttpAlias,
    HttpInboundEndpoint,
    HttpObservation,
    HttpTopologyMatcher,
    KnownExternalRegistry,
    KnownExternalRule,
    normalize_path,
    route_score,
)


def _observation(**overrides):
    values = {
        "observation_id": "http:1",
        "source_member_id": "gateway",
        "source_entry_id": "gateway:entry",
        "method": "GET",
        "path": "/api/users/42",
        "authority": "users.internal:8443",
        "extraction_confidence": 0.95,
    }
    values.update(overrides)
    return HttpObservation(**values)


def _endpoint(**overrides):
    values = {
        "member_id": "users",
        "entry_id": "users:get-user",
        "method": "GET",
        "path": "/api/users/{id}",
    }
    values.update(overrides)
    return HttpInboundEndpoint(**values)


def _aliases():
    return [HttpAlias("users", "https://users.internal:8443", "frozen_config", 0.95)]


def test_http_match_requires_unique_frozen_target_identity_and_compatible_route():
    result = HttpTopologyMatcher().match(_observation(), [_endpoint()], _aliases())

    assert result.status == "confirmed"
    assert result.target_member_id == "users"
    assert result.target_entry_ids == ("users:get-user",)
    assert result.score >= 0.9
    assert result.confidence_level == "high"
    assert [component.name for component in result.confidence_components] == [
        "extraction",
        "reachability",
        "protocol_identity",
        "target_identity",
    ]


def test_relative_url_without_binding_never_uses_route_similarity_as_service_identity():
    result = HttpTopologyMatcher().match(
        _observation(authority=None, client_binding_authority=None),
        [_endpoint()],
        _aliases(),
    )

    assert result.status == "unresolved"
    assert result.target_member_id is None
    assert result.reasons == ("relative_url_without_unique_client_binding",)


def test_explicit_unregistered_authority_is_scope_boundary_not_false_internal_edge():
    result = HttpTopologyMatcher().match(
        _observation(authority="payments.vendor.example"),
        [_endpoint()],
        _aliases(),
    )

    assert result.status == "out_of_scope_or_unregistered"
    assert result.target_member_id is None


def test_known_external_requires_an_explicit_exact_registry_match():
    matcher = HttpTopologyMatcher(
        known_external_registry=KnownExternalRegistry((
            KnownExternalRule(
                "stripe-api",
                "api.stripe.example",
                provider="stripe",
                source="deployment-registry",
            ),
        ))
    )
    result = matcher.match(
        _observation(authority="https://api.stripe.example/v1", path="/charges"),
        [_endpoint()],
        _aliases(),
    )

    assert result.status == "known_external"
    assert result.external_rule_id == "stripe-api"
    assert result.external_provider == "stripe"
    assert result.external_rule_source == "deployment-registry"
    assert result.reasons == ("known_external_registry_match",)


def test_unregistered_authority_does_not_become_known_external_from_provider_like_name():
    matcher = HttpTopologyMatcher(
        known_external_registry=[{"rule_id": "stripe-api", "authority": "api.stripe.example"}]
    )
    result = matcher.match(
        _observation(authority="api.payments.example"),
        [_endpoint()],
        _aliases(),
    )

    assert result.status == "out_of_scope_or_unregistered"
    assert result.external_rule_id is None
    assert result.reasons == ("authority_not_registered_in_view",)


def test_multiple_matching_external_rules_are_ambiguous():
    matcher = HttpTopologyMatcher(
        known_external_registry=[
            {"rule_id": "payments", "authority": "payments.example"},
            {"rule_id": "fraud", "authority": "payments.example"},
        ]
    )
    result = matcher.match(_observation(authority="payments.example"), [_endpoint()], _aliases())

    assert result.status == "ambiguous"
    assert result.candidates == ("fraud", "payments")
    assert result.reasons == ("ambiguous_known_external_rule",)


def test_internal_alias_external_rule_conflict_is_ambiguous():
    matcher = HttpTopologyMatcher(
        known_external_registry=[{"rule_id": "users-external", "authority": "users.internal:8443"}]
    )
    result = matcher.match(_observation(), [_endpoint()], _aliases())

    assert result.status == "ambiguous"
    assert result.reasons == ("internal_alias_conflicts_with_known_external_rule",)


def test_external_rule_constraints_are_explainable_and_do_not_widen_identity():
    matcher = HttpTopologyMatcher(
        known_external_registry=[
            {
                "rule_id": "billing-charges",
                "authority": "billing.example",
                "methods": ["POST"],
                "path_prefixes": ["/charges"],
            }
        ]
    )
    result = matcher.match(
        _observation(authority="billing.example", method="GET", path="/refunds"),
        [_endpoint()],
        _aliases(),
    )

    assert result.status == "out_of_scope_or_unregistered"
    assert result.reasons == ("known_external_rule_constraints_not_matched",)


def test_conflicting_aliases_are_ambiguous_even_when_only_one_route_matches():
    aliases = [
        HttpAlias("users", "users.internal:8443"),
        HttpAlias("orders", "users.internal:8443"),
    ]
    result = HttpTopologyMatcher().match(_observation(), [_endpoint()], aliases)

    assert result.status == "ambiguous"
    assert result.candidates == ("orders", "users")
    assert result.reasons == ("ambiguous_alias",)


def test_method_and_entry_reachability_are_hard_conditions():
    matcher = HttpTopologyMatcher()
    unknown_method = matcher.match(_observation(method=None), [_endpoint()], _aliases())
    unreachable = matcher.match(_observation(entry_reachable=False), [_endpoint()], _aliases())

    assert unknown_method.status == "candidate"
    assert unknown_method.reasons == ("unknown_http_method",)
    assert unreachable.status == "unresolved"
    assert unreachable.reasons == ("missing_entry_reachability",)


def test_tied_endpoints_under_one_identified_service_are_confirmed_alternatives():
    result = HttpTopologyMatcher().match(
        _observation(),
        [_endpoint(entry_id="users:a"), _endpoint(entry_id="users:b")],
        _aliases(),
    )

    assert result.status == "confirmed"
    assert result.target_entry_ids == ("users:a", "users:b")
    assert result.endpoint_alternatives == ("users:b",)


def test_base_path_trailing_slash_and_route_wildcards_are_normalized_without_substring_matching():
    matcher = HttpTopologyMatcher()
    result = matcher.match(
        _observation(path="/api/v1/users/42/"),
        [_endpoint(path="/users/{id}", base_path="/api/v1/")],
        _aliases(),
    )

    assert result.status == "confirmed"
    assert normalize_path("/api/v1/users/42/?token=secret") == "/api/v1/users/42"
    assert route_score("/files/a/b", "/files/**") == 0.866667
    assert route_score("/users/42", "/user/42") is None


def test_mapping_communication_artifact_shape_is_accepted_and_low_confidence_stays_candidate():
    observation = {
        "observation_id": "http:mapping",
        "entry_ref": {"member_id": "gateway", "entry_id": "gateway:entry"},
        "request": {
            "method": "POST",
            "authority": "orders",
            "normalized_path": "/orders",
        },
        "extraction_confidence": 0.10,
    }
    result = HttpTopologyMatcher().match(
        observation,
        [{"member_id": "orders", "entry_id": "orders:create", "method": "POST", "path": "/orders"}],
        [{"member_id": "orders", "authority": "orders", "confidence": 1.0}],
    )

    assert result.status == "candidate"
    assert result.target_member_id == "orders"
    assert result.reasons == ("confidence_below_confirmed_threshold",)
