"""Small HTTP adapter for the local Kimi WebBridge daemon."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class WebBridgeError(RuntimeError):
    pass


class WebBridgeClient:
    def __init__(self, endpoint: str = "http://127.0.0.1:10086/command"):
        self.endpoint = endpoint

    def command(self, session: str, action: str, args: dict | None = None) -> dict:
        payload = json.dumps(
            {"session": session, "action": action, "args": args or {}}, ensure_ascii=False
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=65) as response:
                result = json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            raise WebBridgeError(f"Cannot reach Kimi WebBridge: {error}") from error
        if not result.get("ok"):
            detail = result.get("error") or {}
            raise WebBridgeError(detail.get("message") or "Kimi WebBridge command failed")
        return result.get("data") or {}
