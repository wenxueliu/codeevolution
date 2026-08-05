#!/usr/bin/env python3
"""Real-browser regression for repository-scoped refactoring techniques.

Prerequisites:
  1. CodeHistory Web is running (default: http://127.0.0.1:8765).
  2. Kimi WebBridge daemon and Chrome extension are connected.
  3. At least one logical repository with one CodeGraph-initialized member is registered.

The test intentionally leaves its browser tab open for inspection. Test data is
removed through the public API in ``finally`` even when an assertion fails.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TECHNIQUE_ID = "e2e-refactoring-technique"
TECHNIQUE_NAME = "E2E仓库级重构检查"
EDITED_NAME = "E2E仓库级重构检查（已编辑）"


def http_json(url: str, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = json.loads(error.read().decode("utf-8"))
        return error.code, body


class WebBridge:
    def __init__(self, endpoint: str, session: str):
        self.endpoint = endpoint
        self.session = session

    def command(self, action: str, **args):
        payload = json.dumps(
            {"action": action, "args": args, "session": self.session},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"WebBridge {action} failed: {result.get('error')}")
        return result["data"]

    def evaluate(self, code: str):
        return self.command("evaluate", code=code).get("value")

    def wait_for(self, expression: str, message: str, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.evaluate(f"Boolean({expression})"):
                return
            time.sleep(0.25)
        raise AssertionError(message)


def js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def query_url(base_url: str, path: str, **query) -> str:
    return f"{base_url}{path}?{urllib.parse.urlencode(query)}"


def set_form(bridge: WebBridge, *, technique_id: str, name: str, objective: str, checks: str):
    values = [technique_id, name, objective, checks]
    bridge.evaluate(
        "(()=>{"
        "const fields=[...document.querySelectorAll('.technique-form input,.technique-form textarea')];"
        f"const values={json.dumps(values, ensure_ascii=False)};"
        "fields.forEach((field,index)=>{"
        "const proto=field.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
        "Object.getOwnPropertyDescriptor(proto,'value').set.call(field,values[index]);"
        "field.dispatchEvent(new Event('input',{bubbles:true}));"
        "});return fields.map(field=>field.value)})()"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--repo", default="", help="Logical repository name; default is first")
    parser.add_argument("--member", default="", help="Physical member name; default is first")
    parser.add_argument("--webbridge", default="http://127.0.0.1:10086/command")
    parser.add_argument("--session", default="codehistory-refactoring-e2e")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    status, repositories = http_json(f"{base_url}/api/repos")
    assert status == 200 and repositories.get("repos"), "No registered repository available"
    logical = next(
        (item for item in repositories["repos"] if item["name"] == args.repo),
        repositories["repos"][0] if not args.repo else None,
    )
    assert logical is not None, f"Logical repository '{args.repo}' not found"
    members = [item["name"] for item in logical["repositories"]]
    member = args.member or members[0]
    assert member in members, f"Repository member '{member}' not found"
    other_member = next((item for item in members if item != member), "")

    cleanup_url = query_url(
        base_url,
        f"/api/refactor-techniques/{TECHNIQUE_ID}",
        repo=logical["name"],
        member=member,
    )
    http_json(cleanup_url, "DELETE")  # idempotent pre-clean

    bridge = WebBridge(args.webbridge, args.session)
    page_url = f"{base_url}/#/repo/{urllib.parse.quote(logical['name'])}/refactoring"
    try:
        bridge.command(
            "navigate",
            url=page_url,
            newTab=True,
            group_title="CodeHistory 重构回归",
        )
        bridge.wait_for(
            "document.querySelectorAll('.filters select')[0]?.options.length > 0",
            "Repository selector did not load",
        )
        bridge.evaluate(
            "(()=>{const e=document.querySelectorAll('.filters select')[0];"
            f"e.value={js_string(member)};e.dispatchEvent(new Event('change',{{bubbles:true}}));return e.value}})()"
        )
        bridge.wait_for(
            f"document.querySelectorAll('.filters select')[0]?.value==={js_string(member)}",
            "Target repository member was not selected",
        )

        bridge.command("click", selector=".header-actions .secondary")
        bridge.wait_for("document.querySelector('.technique-form')", "Create form did not open")
        set_form(
            bridge,
            technique_id=TECHNIQUE_ID,
            name=TECHNIQUE_NAME,
            objective="验证仓库级新增、编辑和隔离",
            checks="检查仓库隔离\n检查编辑持久化",
        )
        bridge.command("click", selector=".technique-form button.primary")
        bridge.wait_for(
            f"document.body.innerText.includes({js_string(TECHNIQUE_NAME)}) && !document.querySelector('.technique-form')",
            "Created technique was not rendered",
        )

        bridge.command("click", selector=".edit-button")
        bridge.wait_for(
            "document.querySelector('.technique-form input')?.disabled",
            "Edit form did not lock the stable technique id",
        )
        bridge.evaluate(
            "(()=>{const field=document.querySelectorAll('.technique-form input')[1];"
            f"Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(field,{js_string(EDITED_NAME)});"
            "field.dispatchEvent(new Event('input',{bubbles:true}));return field.value})()"
        )
        bridge.command("click", selector=".technique-form button.primary")
        bridge.wait_for(
            f"document.body.innerText.includes({js_string(EDITED_NAME)}) && !document.querySelector('.technique-form')",
            "Edited technique was not rendered",
        )

        if other_member:
            bridge.evaluate(
                "(()=>{const e=document.querySelectorAll('.filters select')[0];"
                f"e.value={js_string(other_member)};e.dispatchEvent(new Event('change',{{bubbles:true}}));return e.value}})()"
            )
            bridge.wait_for(
                f"document.querySelectorAll('.filters select')[0]?.value==={js_string(other_member)}",
                "Other repository member was not selected",
            )
            bridge.wait_for(
                f"!document.body.innerText.includes({js_string(EDITED_NAME)})",
                "Custom technique leaked into another repository member",
            )
        assert not bridge.evaluate("Boolean(document.querySelector('.request-error'))"), "Page error shown"
        print(
            f"PASS: create/edit/isolation in real Chrome for {logical['name']}/{member}"
        )
    finally:
        cleanup_status, cleanup = http_json(cleanup_url, "DELETE")
        if cleanup_status not in {200, 404}:
            raise RuntimeError(f"Failed to clean E2E technique: {cleanup}")


if __name__ == "__main__":
    main()
