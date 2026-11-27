from types import SimpleNamespace

from codeevolution.application.rule_map_service import RuleMapService, normalize_rule_text


class _Store:
    def __init__(self):
        self.nodes = {
            "GET|/order/list|OrderController.list": {
                "business_rules": [
                    {"rule": "订单列表只返回当前用户有权限的订单", "rule_type": "authorization",
                     "evidence_lines": [42]},
                    {"rule": "订单金额必须大于 0", "rule_type": "validation",
                     "condition": "amount <= 0", "failure_mode": "400"},
                ],
                "integration_contracts": [
                    {"kind": "http", "target": "member-service",
                     "operation": "GET /member/{id}", "input_or_key": "userId",
                     "result_usage": "校验会员", "evidence_lines": [12]},
                ],
            },
            "GET|/order/detail|OrderController.detail": {
                "business_rules": [
                    {"rule": "订单列表只返回当前用户有权限的订单。", "rule_type": "authorization",
                     "evidence_lines": [55]},
                    {"rule": "已取消订单不允许再次支付", "rule_type": "workflow"},
                ],
                "integration_contracts": [
                    {"kind": "database", "target": "order 表", "operation": "SELECT"},
                ],
            },
        }
        self.current = ""

    def list_snapshots_for_repository_snapshot(self, snapshot_id, key):
        if key in self.nodes:
            self.current = key
            return [SimpleNamespace(id="explanation", entry_node_key="node")]
        return []

    def get_node(self, snapshot_id, node_key):
        return SimpleNamespace(
            aggregate_explanation=self.nodes.get(self.current, {}),
            local_explanation={},
        )


class _Queries:
    def api_contract(self, snapshot_id):
        return {
            "endpoints": [
                {"method": "GET", "path": "/order/list", "handler": "OrderController.list",
                 "file": "OrderController.java", "line": 10},
                {"method": "GET", "path": "/order/detail", "handler": "OrderController.detail",
                 "file": "OrderController.java", "line": 20},
                {"method": "POST", "path": "/user/login", "handler": "UserController.login",
                 "file": "UserController.java", "line": 30},
            ]
        }


def test_rule_map_dedupes_group_rules_and_keeps_provenance():
    result = RuleMapService(_Queries(), _Store()).build("snap-1")

    assert result["summary"]["interface_count"] == 3
    assert result["summary"]["interfaces_with_rules"] == 2
    assert result["summary"]["rule_count"] == 3
    assert result["summary"]["shared_rule_count"] == 1
    assert result["summary"]["coverage"] == 0.6667

    order = next(group for group in result["groups"] if group["group_key"] == "order")
    shared = next(rule for rule in order["rules"] if rule["role"] == "shared")
    assert shared["text"] == "订单列表只返回当前用户有权限的订单"
    assert len(shared["source_interfaces"]) == 2
    assert {item["line"] for item in shared["evidence"]} == {10, 20}

    assert result["service"]["rule_count"] == 3
    assert result["integrations"]["total"] == 2
    assert result["integrations"]["cross_service_count"] == 1


def test_rule_map_normalizes_text_before_deduping():
    assert normalize_rule_text("订单金额必须大于 0。") == normalize_rule_text(" 订单金额必须大于 0 ")
    assert normalize_rule_text("阈值 ≥ 10") == "阈值 >= 10"


def test_rule_map_carries_interface_rules_and_integrations():
    result = RuleMapService(_Queries(), _Store()).build("snap-1")

    interface = next(item for item in result["interfaces"] if item["path"] == "/order/list")
    assert interface["rule_count"] == 2
    assert len(interface["rules"]) == 2
    assert result["integrations"]["items"][0]["source_interface"].startswith("GET|/order")
    assert result["integrations"]["by_kind"] == [
        {"kind": "database", "count": 1},
        {"kind": "http", "count": 1},
    ]
