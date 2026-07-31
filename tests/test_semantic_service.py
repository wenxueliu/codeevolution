from types import SimpleNamespace

from codehistory import llm
from codehistory.semantic.json_parser import complete_json, parse_json
from codehistory.semantic.models import BusinessDescription
from codehistory.semantic.service import SemanticService


class FakeClient:
    def complete(self, prompt, **options):
        assert prompt == "prompt"
        return '```json\n{"ok": true}\n```'


def test_complete_json_is_public_and_transport_independent():
    assert complete_json(FakeClient(), "prompt") == {"ok": True}
    assert parse_json("not json") == {"raw": "not json"}
    assert parse_json(None) is None


def test_legacy_models_and_parser_are_reexports():
    assert llm.BusinessDescription is BusinessDescription
    assert llm._parse_json('{"ok": true}') == {"ok": True}


def test_semantic_service_uses_injected_client():
    assert SemanticService(FakeClient()).complete_json("prompt") == {"ok": True}


def test_batch_explain_honors_concurrency_and_preserves_order(monkeypatch):
    workers = []

    class FakeExecutor:
        def __init__(self, max_workers):
            workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def map(self, function, values):
            return map(function, values)

    monkeypatch.setattr(llm, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(
        llm,
        "explain_business_purpose",
        lambda func_name, **kwargs: SimpleNamespace(
            function_name=func_name,
            summary_en=func_name,
            summary_zh=func_name,
            business_domain="test",
            role="test",
            key_responsibilities=[],
        ),
    )
    progress = []
    result = llm.batch_explain_functions(
        [{"name": "first"}, {"name": "second"}],
        max_concurrency=2,
        progress_callback=lambda current, total: progress.append((current, total)),
    )
    assert workers == [2]
    assert [item["function_name"] for item in result] == ["first", "second"]
    assert progress == [(1, 2), (2, 2)]
