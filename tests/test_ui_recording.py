from codehistory.application.ui_recording_service import (
    UiRecordingService,
    find_reference,
    normalize_actions,
    origin,
)
from codehistory.infrastructure.ui_test_store import UiTestStore


class BridgeStub:
    def __init__(self, actions=None, missing=False):
        self.calls = []
        self.actions = actions or []
        self.missing = missing

    def command(self, session, action, args=None):
        self.calls.append((session, action, args or {}))
        if action == "evaluate" and "window.__codehistoryRecorder.export()" in (args or {}).get("code", ""):
            result, self.actions = self.actions, []
            return {"type": "object", "value": result}
        if action == "evaluate" and "querySelectorAll('select')" in (args or {}).get("code", ""):
            return {"type": "boolean", "value": True}
        if action == "network" and (args or {}).get("cmd") == "list":
            return {"requests": [{"method": "POST", "url": "http://shop.test/api/orders?token=x", "status": 200}]}
        if action == "snapshot":
            if self.missing:
                return {"tree": []}
            return {"tree": [{"role": "button", "name": "保存", "ref": "@e9"}]}
        if action == "screenshot":
            return {"path": "/tmp/failed.jpg"}
        return {"success": True}


def test_external_ui_recording_collects_steps_network_and_replays(tmp_path):
    store = UiTestStore(str(tmp_path / "ui.db"))
    bridge = BridgeStub(
        [
            {"action": "fill", "target": {"strategy": "role-name", "role": "textbox", "name": "名称"}, "value": "a", "url": "http://shop.test/add", "timestamp": 1000},
            {"action": "fill", "target": {"strategy": "role-name", "role": "textbox", "name": "名称"}, "value": "ab", "url": "http://shop.test/add", "timestamp": 1100},
            {"action": "click", "target": {"strategy": "role-name", "role": "button", "name": "保存"}, "url": "http://shop.test/add", "timestamp": 1200},
        ]
    )
    service = UiRecordingService(store, bridge, locator_attempts=1)
    target = service.add_target("mall", "shop", "http://shop.test", [])
    recording = service.start("mall", target["id"], "创建商品", "http://shop.test/add")
    collected = service.collect(recording["id"])
    assert [step["action"] for step in collected["steps"]] == ["fill", "click"]
    assert collected["steps"][0]["payload"]["value"] == "ab"

    stopped = service.stop(recording["id"])
    assert stopped["network_log"] == [{"method": "POST", "path": "/api/orders", "status": 200}]
    run = service.replay(recording["id"])
    assert run["status"] == "failed"  # recorded textbox is absent from the replay snapshot
    assert run["screenshot_path"] == "/tmp/failed.jpg"


def test_replay_passes_with_testid_selectors_and_native_selects(tmp_path):
    store = UiTestStore(str(tmp_path / "ui.db"))
    bridge = BridgeStub([
        {"action": "click", "target": {"strategy": "testid", "value": "save"}, "url": "http://shop.test", "timestamp": 1000},
        {"action": "select", "target": {"strategy": "testid", "value": "status"}, "value": "active", "url": "http://shop.test", "timestamp": 1100},
    ])
    service = UiRecordingService(store, bridge)
    target = service.add_target("mall", "shop", "http://shop.test", [])
    recording = service.start("mall", target["id"], "保存", "http://shop.test")
    service.stop(recording["id"])
    run = service.replay(recording["id"])
    assert run["status"] == "passed"
    assert any(action == "click" and args["selector"] == '[data-testid="save"]' for _, action, args in bridge.calls)
    assert any(action == "evaluate" and "active" in args["code"] for _, action, args in bridge.calls)


def test_target_origin_and_snapshot_matching_are_strict(tmp_path):
    service = UiRecordingService(UiTestStore(str(tmp_path / "ui.db")), BridgeStub())
    target = service.add_target("mall", "shop", "https://shop.test/base", [])
    assert origin("https://shop.test/a") == "https://shop.test"
    try:
        service.start("mall", target["id"], "bad", "https://evil.test")
    except ValueError as error:
        assert "not allowed" in str(error)
    else:
        raise AssertionError("foreign origins must be rejected")
    assert find_reference([{"role": "button", "name": "保存", "ref": "@e1"}], {"strategy": "role-name", "role": "button", "name": "保存"}) == "@e1"
    assert normalize_actions([]) == []
