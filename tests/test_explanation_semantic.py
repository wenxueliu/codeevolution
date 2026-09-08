from codeevolution.semantic.explanation_service import ExplanationSemanticService


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def complete(self, prompt, **_options):
        self.prompts.append(prompt)
        return next(self.responses)


def test_single_chunk_becomes_local_and_leaf_aggregate_without_extra_calls():
    client = Client(['{"summary":"校验订单","business_rules":["必须登录"]}'])
    service = ExplanationSemanticService(client)
    node = {"qualified_name": "Order.create", "file": "order.py"}
    explained = service.explain_chunk(
        node, {"line_start": 10, "line_end": 20, "source": "if not user: raise Error"}
    )
    local = service.synthesize_local(node, [{"explanation": explained}])
    aggregate = service.aggregate_node(node, local, [])
    assert aggregate["summary"] == "校验订单"
    assert aggregate["business_rules"] == ["必须登录"]
    assert aggregate["children"] == []
    assert len(client.prompts) == 1


def test_parent_aggregation_passes_child_facts_and_references():
    client = Client(
        [
            '{"summary":"父节点"}',
            '{"summary":"完整流程","side_effects":["写数据库"]}',
        ]
    )
    service = ExplanationSemanticService(client)
    node = {"qualified_name": "Order.create", "file": "order.py"}
    local = service.explain_chunk(
        node, {"line_start": 1, "line_end": 2, "source": "repo.save(order)"}
    )
    aggregate = service.aggregate_node(
        node,
        local,
        [{"node_key": "Repo.save", "call_line": 2, "aggregate": {"summary": "保存", "side_effects": ["写数据库"]}}],
    )
    assert aggregate["summary"] == "完整流程"
    assert aggregate["children"] == [{"node_key": "Repo.save", "call_line": 2}]
    assert "Repo.save" in client.prompts[-1]
