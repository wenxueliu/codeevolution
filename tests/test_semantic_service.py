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
