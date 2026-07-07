"""anyclient — typed HTTP client for the `any` server (localhost).

M2 scope: the transport wrapper (JSON, error mapping, the NUL guard)
plus the handful of calls config needs; grows in M4. The transport is
injectable (`send(method, path, body) -> (status, json)`) so the client
is unit-testable with no server.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[str, str, dict | None], tuple[int, dict]]


def sanitize_nuls(obj: Any) -> Any:
    """Strip NUL bytes from strings before any dataset write. anyenc/
    fastjson rejects \\x00 in JSON strings and we deliberately don't
    fork upstream — guard at the write boundary (docs/m0-notes.md,
    project gotcha). Binary-ish HTTP bodies are the realistic source."""
    if isinstance(obj, str):
        return obj.replace("\x00", "�") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: sanitize_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_nuls(v) for v in obj]
    return obj


class AnyError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


def http_transport(base_url: str) -> Transport:
    def send(method: str, path: str, body: dict | None) -> tuple[int, dict]:
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (_json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (_json.loads(raw) if raw else {})

    return send


class AnyClient:
    def __init__(self, transport: Transport):
        self._send = transport

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if body is not None:
            body = sanitize_nuls(body)   # write guard
        status, data = self._send(method, path, body)
        if status >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(status, err.get("code", "unknown"), err.get("message", ""))
        return data

    # --- the calls config needs now (grows in M4) ---
    def query(self, space_id: str, object_id: str, dataset: str, **body) -> list[dict]:
        r = self._call("POST", f"/v1/spaces/{space_id}/query",
                       {"objectId": object_id, "dataset": dataset, **body})
        return r.get("records", [])

    def modify(self, space_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/modify", body)
