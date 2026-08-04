"""Record and replay external-system UI flows through Kimi WebBridge."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..infrastructure.webbridge_client import WebBridgeError

RECORDER_SCRIPT = r"""(() => {
  if (window.__codehistoryRecorder) return 'already-installed';
  const marker='__CODEHISTORY__:';
  let named=null; try { if(window.name.startsWith(marker)) named=JSON.parse(window.name.slice(marker.length)); } catch(_error) {}
  const originalName=named?.original??window.name;
  const restored = named?.actions || JSON.parse(sessionStorage.getItem('__codehistory_recording') || '[]');
  const sensitive = /password|token|secret|authorization|cookie|api.?key/i;
  const roleOf = el => el.getAttribute('role') || ({BUTTON:'button',A:'link',INPUT:'textbox',TEXTAREA:'textbox',SELECT:'combobox'})[el.tagName] || '';
  const nameOf = el => (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().replace(/\s+/g,' ').slice(0,100);
  const targetOf = source => {
    const el = source.closest?.('[data-testid],button,a,input,textarea,select,[role]') || source;
    if (el.dataset?.testid) return {strategy:'testid',value:el.dataset.testid};
    return {strategy:'role-name',role:roleOf(el),name:nameOf(el)};
  };
  const state = {actions:restored};
  const persist=()=>{sessionStorage.setItem('__codehistory_recording',JSON.stringify(state.actions)); window.name=marker+JSON.stringify({original:originalName,actions:state.actions});};
  const save = item => { state.actions.push({...item,url:location.href,timestamp:Date.now()}); persist(); };
  const click = event => { const el=event.target.closest?.('a,button,[role]')||event.target; if(el.tagName==='A'&&el.target==='_blank') save({action:'new-tab',target:targetOf(el),value:el.href}); else save({action:'click',target:targetOf(el)}); };
  const change = event => { const el=event.target; if(el.type==='file') save({action:'upload',target:targetOf(el),files:[...el.files].map(file=>file.name)}); else { const hidden=el.type==='password'||sensitive.test(el.name||''); save({action:el.tagName==='SELECT'?'select':'fill',target:targetOf(el),value:hidden?'<redacted>':el.value,...(hidden?{secret:'UI_SECRET_'+(el.name||nameOf(el)||'VALUE').replace(/\W+/g,'_').toUpperCase()}:{})}); } };
  let dragSource=null; const dragstart=event=>{dragSource=targetOf(event.target)}; const drop=event=>{if(dragSource) save({action:'drag',target:dragSource,destination:targetOf(event.target)}); dragSource=null};
  const navigation = () => save({action:'navigate',target:{},value:location.href});
  document.addEventListener('click',click,true); document.addEventListener('change',change,true);
  document.addEventListener('dragstart',dragstart,true); document.addEventListener('drop',drop,true);
  window.addEventListener('hashchange',navigation); window.addEventListener('popstate',navigation);
  for (const method of ['pushState','replaceState']) { const original=history[method]; history[method]=function(...args){ const result=original.apply(this,args); navigation(); return result; }; }
  state.export = () => { const result=state.actions.splice(0); persist(); return {actions:result,url:location.href}; };
  state.stop = () => { document.removeEventListener('click',click,true); document.removeEventListener('change',change,true); document.removeEventListener('dragstart',dragstart,true); document.removeEventListener('drop',drop,true); window.removeEventListener('hashchange',navigation); window.removeEventListener('popstate',navigation); window.name=originalName; };
  window.__codehistoryRecorder=state; return 'installed';
})()"""

EXPORT_SCRIPT = f"""(() => {{
  if (!window.__codehistoryRecorder) {RECORDER_SCRIPT};
  return window.__codehistoryRecorder.export();
}})()"""
STOP_SCRIPT = "(() => { if(window.__codehistoryRecorder) window.__codehistoryRecorder.stop(); return true })()"


class UiRecordingService:
    def __init__(self, store, bridge, locator_attempts: int = 20):
        self.store = store
        self.bridge = bridge
        self.locator_attempts = locator_attempts

    def add_target(self, repository: str, name: str, base_url: str, origins: list[str]):
        base_origin = origin(base_url)
        allowed = list(dict.fromkeys([base_origin, *(origin(item) for item in origins)]))
        return self.store.add_target(repository, name, base_url, allowed)

    def start(self, repository: str, target_id: int, name: str, start_url: str):
        target = self._target(target_id, repository)
        self._validate_url(start_url, target)
        session = f"ui-recording-{int(time.time() * 1000)}"
        self.bridge.command(
            session,
            "navigate",
            {"url": start_url, "newTab": True, "group_title": f"UI 录制：{name}"},
        )
        self.bridge.command(session, "network", {"cmd": "start"})
        self.bridge.command(
            session,
            "cdp",
            {"method": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": RECORDER_SCRIPT}},
        )
        self.bridge.command(session, "evaluate", {"code": RECORDER_SCRIPT})
        return self.store.create_recording(repository, target_id, name, start_url, session)

    def collect(self, recording_id: int):
        recording = self._recording(recording_id)
        if recording["status"] != "recording":
            return recording
        target = self._target(recording["target_id"], recording["repository"])
        tabs = self.bridge.command(recording["webbridge_session"], "list_tabs")
        for tab in tabs.get("tabs", []):
            if tab.get("url", "").startswith(("http://", "https://")):
                self._validate_url(tab["url"], target)
        data = self.bridge.command(
            recording["webbridge_session"], "evaluate", {"code": EXPORT_SCRIPT}
        )
        exported = data.get("value") if isinstance(data, dict) else {}
        exported = exported if isinstance(exported, dict) else {"actions": exported or []}
        if exported.get("url"):
            self._validate_url(exported["url"], target)
        self.store.append_steps(recording_id, normalize_actions(exported.get("actions") or []))
        return self.store.get_recording(recording_id)

    def add_checkpoint(
        self, recording_id: int, action: str, target: dict, payload: dict, page_url: str = ""
    ):
        recording = self._recording(recording_id)
        allowed = {
            "assert-visible", "assert-url", "assert-response", "fixture", "upload", "drag"
        }
        if action not in allowed:
            raise ValueError("Unsupported UI checkpoint action")
        target_config = self._target(recording["target_id"], recording["repository"])
        for url in (page_url, payload.get("url", "")):
            if url:
                self._validate_url(url, target_config)
        return self.store.append_checkpoint(recording_id, action, target, payload, page_url)

    def stop(self, recording_id: int):
        recording = self.collect(recording_id)
        session = recording["webbridge_session"]
        try:
            network = self.bridge.command(session, "network", {"cmd": "list", "filter": "/api/"})
            network_log = sanitize_network(network)
        except WebBridgeError:
            network_log = []
        self.bridge.command(session, "evaluate", {"code": STOP_SCRIPT})
        try:
            self.bridge.command(session, "network", {"cmd": "stop"})
        except WebBridgeError:
            pass
        return self.store.finish_recording(recording_id, network_log)

    def replay(self, recording_id: int):
        recording = self._recording(recording_id)
        target = self._target(recording["target_id"], recording["repository"])
        self._validate_url(recording["start_url"], target)
        session = f"ui-test-run-{recording_id}-{int(time.time())}"
        run = self.store.create_run(recording_id, session)
        started = time.perf_counter()
        current = 0
        try:
            self.bridge.command(
                session,
                "navigate",
                {
                    "url": recording["start_url"],
                    "newTab": True,
                    "group_title": f"UI 测试：{recording['name']}",
                },
            )
            self.bridge.command(session, "network", {"cmd": "start"})
            for step in recording["steps"]:
                current = step["sequence"]
                self._execute(session, step, target)
            self._stop_network(session)
            return self.store.finish_run(
                run["id"], "passed", current_step=current,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as error:
            self._stop_network(session)
            screenshot = ""
            try:
                screenshot = self.bridge.command(
                    session, "screenshot", {"format": "jpeg", "quality": 85}
                ).get("path", "")
            except WebBridgeError:
                pass
            return self.store.finish_run(
                run["id"], "failed", current_step=current,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(error), screenshot_path=screenshot,
            )

    def _stop_network(self, session: str):
        try:
            self.bridge.command(session, "network", {"cmd": "stop"})
        except WebBridgeError:
            pass

    def _execute(self, session: str, step: dict, target: dict):
        action = step["action"]
        payload = step["payload"]
        if action == "navigate":
            url = payload.get("value") or step["page_url"]
            self._validate_url(url, target)
            self.bridge.command(session, "navigate", {"url": url})
            return
        if action == "new-tab":
            url = payload.get("value") or step["page_url"]
            self._validate_url(url, target)
            self.bridge.command(session, "navigate", {"url": url, "newTab": True})
            return
        if action == "assert-url":
            current = self.bridge.command(session, "evaluate", {"code": "location.href"}).get("value", "")
            expected = payload.get("value", "")
            if expected not in current:
                raise AssertionError(f"URL does not contain {expected!r}: {current}")
            return
        if action == "assert-response":
            network = self.bridge.command(session, "network", {"cmd": "list", "filter": payload.get("path", "")})
            rows = sanitize_network(network)
            expected_status = int(payload.get("status", 200))
            if not any(row["path"] == payload.get("path") and row["status"] == expected_status for row in rows):
                raise AssertionError(f"Expected response not found: {payload}")
            return
        if action == "fixture":
            url = payload.get("url", "")
            self._validate_url(url, target)
            result = self.bridge.command(session, "evaluate", {"code": fixture_script(payload)}).get("value")
            if not result or not result.get("ok"):
                raise AssertionError(f"Fixture request failed: {result}")
            return
        if action == "assert-visible":
            snapshot = self.bridge.command(session, "snapshot")
            if not tree_contains(snapshot.get("tree", []), step["target"]):
                raise AssertionError(f"Visible element not found: {step['target']}")
            return
        if action not in {"click", "fill", "select", "upload", "drag"}:
            raise ValueError(f"Unsupported recorded UI action: {action}")
        target_spec = step["target"]
        if target_spec.get("strategy") == "testid":
            escaped = str(target_spec.get("value", "")).replace("\\", "\\\\").replace('"', '\\"')
            reference = f'[data-testid="{escaped}"]'
        else:
            reference = None
            for _attempt in range(self.locator_attempts):
                snapshot = self.bridge.command(session, "snapshot")
                reference = find_reference(snapshot.get("tree", []), target_spec)
                if reference:
                    break
                time.sleep(0.25)
        if not reference:
            raise AssertionError(f"Element not found: {step['target']}")
        if action == "click":
            self.bridge.command(session, "click", {"selector": reference})
        elif action == "fill":
            value = payload.get("value", "")
            if value == "<redacted>":
                secret_name = payload.get("secret", "")
                value = os.environ.get(secret_name, "")
                if not value:
                    raise ValueError(f"Missing replay secret: {secret_name}")
            self.bridge.command(session, "fill", {"selector": reference, "value": value})
        elif action == "select":
            value = payload.get("value", "")
            code = select_script(target_spec, value)
            changed = self.bridge.command(session, "evaluate", {"code": code}).get("value")
            if not changed:
                raise AssertionError(f"Select not found: {target_spec}")
        elif action == "upload":
            files = validate_upload_files(payload.get("files") or [])
            self.bridge.command(session, "upload", {"selector": reference, "files": files})
        elif action == "drag":
            destination = payload.get("destination") or {}
            boxes = self.bridge.command(
                session, "evaluate", {"code": drag_boxes_script(target_spec, destination)}
            ).get("value")
            if not boxes:
                raise AssertionError("Drag source or destination not found")
            dispatch_drag(self.bridge, session, boxes)

    def _target(self, target_id: int, repository: str):
        target = self.store.get_target(target_id)
        if not target or target["repository"] != repository:
            raise ValueError("UI test target not found")
        return target

    def _recording(self, recording_id: int):
        recording = self.store.get_recording(recording_id)
        if not recording:
            raise ValueError("UI recording not found")
        return recording

    @staticmethod
    def _validate_url(url: str, target: dict):
        if origin(url) not in target["allowed_origins"]:
            raise ValueError("URL origin is not allowed for this UI test target")


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("A valid HTTP(S) URL is required")
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_actions(actions: list[dict]) -> list[dict]:
    result = []
    for action in actions:
        if action.get("action") == "fill" and result and result[-1].get("action") == "fill" and result[-1].get("target") == action.get("target"):
            result[-1] = action
        else:
            result.append(action)
    return result


def find_reference(tree, target: dict):
    if isinstance(tree, list):
        for item in tree:
            if found := find_reference(item, target):
                return found
        return None
    if not isinstance(tree, dict):
        return None
    if target.get("strategy") == "role-name":
        if tree.get("role") == target.get("role") and normalized_name(tree.get("name", "")) == normalized_name(target.get("name", "")):
            return tree.get("ref")
    for child in tree.get("children", []):
        if found := find_reference(child, target):
            return found
    return None


def tree_contains(tree, target: dict) -> bool:
    if isinstance(tree, list):
        return any(tree_contains(item, target) for item in tree)
    if not isinstance(tree, dict):
        return False
    if target.get("strategy") == "text":
        if normalized_name(target.get("value", "")) in normalized_name(tree.get("name", "")):
            return True
    elif target.get("strategy") == "role-name":
        if tree.get("role") == target.get("role") and normalized_name(tree.get("name", "")) == normalized_name(target.get("name", "")):
            return True
    return tree_contains(tree.get("children", []), target)


def sanitize_network(value) -> list:
    items = value.get("requests", value.get("items", [])) if isinstance(value, dict) else []
    result = []
    for item in items[:200]:
        url = item.get("url", "")
        parsed = urlsplit(url)
        result.append(
            {
                "method": item.get("method", "GET"),
                "path": parsed.path,
                "status": item.get("status") or item.get("statusCode"),
            }
        )
    return result


def normalized_name(value: str) -> str:
    return " ".join(str(value).split())


def select_script(target: dict, value: str) -> str:
    """Build a JSON-escaped selector script for native select elements."""
    target_json = json.dumps(target, ensure_ascii=False)
    value_json = json.dumps(value, ensure_ascii=False)
    return f"""(() => {{
      const target={target_json}, value={value_json};
      const roleOf=el=>el.getAttribute('role')||({{SELECT:'combobox'}})[el.tagName]||'';
      const nameOf=el=>(el.getAttribute('aria-label')||el.getAttribute('title')||el.innerText||el.getAttribute('name')||'').trim().replace(/\\s+/g,' ').slice(0,100);
      const elements=[...document.querySelectorAll('select')];
      const el=target.strategy==='testid'
        ? document.querySelector(`[data-testid="${{CSS.escape(target.value)}}"]`)
        : elements.find(item=>roleOf(item)===target.role&&nameOf(item)===target.name);
      if(!el) return false; el.value=value;
      el.dispatchEvent(new Event('input',{{bubbles:true}})); el.dispatchEvent(new Event('change',{{bubbles:true}}));
      return true;
    }})()"""


def fixture_script(payload: dict) -> str:
    url = json.dumps(payload.get("url", ""), ensure_ascii=False)
    method = json.dumps(str(payload.get("method", "POST")).upper())
    body = json.dumps(payload.get("body"), ensure_ascii=False)
    return f"""(async () => {{
      const body={body};
      const response=await fetch({url},{{method:{method},credentials:'include',
        headers:{{'Content-Type':'application/json'}},body:body===null?undefined:JSON.stringify(body)}});
      return {{ok:response.ok,status:response.status}};
    }})()"""


def validate_upload_files(files: list[str]) -> list[str]:
    configured = os.environ.get("CODEHISTORY_UI_UPLOAD_ROOT", "")
    if not configured:
        raise ValueError("Set CODEHISTORY_UI_UPLOAD_ROOT before replaying uploads")
    root = Path(configured).resolve()
    result = []
    for value in files:
        path = Path(value)
        path = path if path.is_absolute() else root / path
        resolved = path.resolve()
        if root not in resolved.parents and resolved != root:
            raise ValueError(f"Upload file is outside CODEHISTORY_UI_UPLOAD_ROOT: {value}")
        if not resolved.is_file():
            raise ValueError(f"Upload file not found: {value}")
        result.append(str(resolved))
    return result


def locator_expression(target: dict) -> str:
    encoded = json.dumps(target, ensure_ascii=False)
    return f"""(target => {{
      if(target.strategy==='testid') return document.querySelector(`[data-testid="${{CSS.escape(target.value)}}"]`);
      const roleOf=el=>el.getAttribute('role')||({{BUTTON:'button',A:'link',INPUT:'textbox',TEXTAREA:'textbox',SELECT:'combobox'}})[el.tagName]||'';
      const nameOf=el=>(el.getAttribute('aria-label')||el.getAttribute('title')||el.innerText||el.getAttribute('placeholder')||el.getAttribute('name')||'').trim().replace(/\\s+/g,' ').slice(0,100);
      return [...document.querySelectorAll('button,a,input,textarea,select,[role]')].find(el=>roleOf(el)===target.role&&nameOf(el)===target.name);
    }})({encoded})"""


def drag_boxes_script(source: dict, destination: dict) -> str:
    source_expression = locator_expression(source)
    destination_expression = locator_expression(destination)
    return f"""(() => {{
      const source={source_expression}, destination={destination_expression};
      if(!source||!destination) return null;
      const a=source.getBoundingClientRect(), b=destination.getBoundingClientRect();
      return {{from:{{x:a.left+a.width/2,y:a.top+a.height/2}},to:{{x:b.left+b.width/2,y:b.top+b.height/2}}}};
    }})()"""


def dispatch_drag(bridge, session: str, boxes: dict) -> None:
    start, end = boxes["from"], boxes["to"]
    bridge.command(
        session,
        "cdp",
        {"method": "Input.dispatchMouseEvent", "params": {"type": "mouseMoved", **start}},
    )
    bridge.command(
        session,
        "cdp",
        {
            "method": "Input.dispatchMouseEvent",
            "params": {"type": "mousePressed", "button": "left", "clickCount": 1, **start},
        },
    )
    bridge.command(
        session,
        "cdp",
        {"method": "Input.dispatchMouseEvent", "params": {"type": "mouseMoved", **end}},
    )
    bridge.command(
        session,
        "cdp",
        {
            "method": "Input.dispatchMouseEvent",
            "params": {"type": "mouseReleased", "button": "left", "clickCount": 1, **end},
        },
    )
