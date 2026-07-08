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

    # --- spaces ---
    def list_spaces(self, status: str | None = None) -> list[dict]:
        path = "/v1/spaces" + (f"?status={status}" if status else "")
        return self._call("GET", path).get("spaces", [])

    def get_space(self, space_id: str) -> dict:
        return self._call("GET", f"/v1/spaces/{space_id}")

    # --- objects ---
    def create_object(self, space_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/objects", body)

    def delete_object(self, space_id: str, object_id: str) -> dict:
        return self._call("DELETE", f"/v1/spaces/{space_id}/objects/{object_id}")

    def query_objects(self, space_id: str, **body) -> list[dict]:
        """Cross-object query over the per-space objects collection."""
        return self._call("POST", f"/v1/spaces/{space_id}/objects/query", body).get("records", [])

    def query(self, space_id: str, object_id: str, dataset: str, **body) -> list[dict]:
        """Per-object dataset query (chat_messages, editor_blocks, …)."""
        r = self._call("POST", f"/v1/spaces/{space_id}/query",
                       {"objectId": object_id, "dataset": dataset, **body})
        return r.get("records", [])

    def modify(self, space_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/modify", body)

    # --- editor markdown (content, NOT markdown — wire landmine) ---
    def get_markdown(self, space_id: str, object_id: str) -> str:
        r = self._call("GET", f"/v1/spaces/{space_id}/objects/{object_id}/editor/markdown")
        return r.get("content", "")

    def put_markdown(self, space_id: str, object_id: str, content: str) -> dict:
        return self._call("PUT", f"/v1/spaces/{space_id}/objects/{object_id}/editor/markdown",
                          {"content": content})

    # --- types & properties (catalog source) ---
    def list_types(self, space_id: str) -> list[dict]:
        return self._call("GET", f"/v1/spaces/{space_id}/types").get("types", [])

    def list_properties(self, space_id: str, type_id: str) -> list[dict]:
        # [{id, name, xKey, kind}] — the xKey↔propId catalog map.
        r = self._call("GET", f"/v1/spaces/{space_id}/types/{type_id}/properties")
        return r.get("properties", r) if isinstance(r, dict) else r

    def set_properties(self, space_id: str, object_id: str, type_id: str, patch: dict) -> dict:
        return self._call("POST",
                          f"/v1/spaces/{space_id}/properties/{object_id}/set/{type_id}",
                          {"patch": patch})

    # --- agent turns / chunks (v2, server-assigned seq) ---
    def append_turn(self, space_id: str, object_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/objects/{object_id}/agent/turns", body)

    def create_chunk(self, space_id: str, object_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/objects/{object_id}/agent/chunks", body)

    # --- chat messages ---
    def chat_send(self, space_id: str, object_id: str, body: dict) -> dict:
        return self._call("POST",
                          f"/v1/spaces/{space_id}/objects/{object_id}/chat/messages", body)

    def chat_edit(self, space_id: str, object_id: str, msg_id: str, text: str) -> dict:
        return self._call("PATCH",
                          f"/v1/spaces/{space_id}/objects/{object_id}/chat/messages/{msg_id}",
                          {"text": text})

    def chat_delete(self, space_id: str, object_id: str, msg_id: str) -> dict:
        return self._call("DELETE",
                          f"/v1/spaces/{space_id}/objects/{object_id}/chat/messages/{msg_id}")

    def chat_react(self, space_id: str, object_id: str, msg_id: str, emoji: str) -> dict:
        return self._call("POST",
                          f"/v1/spaces/{space_id}/objects/{object_id}/chat/messages/{msg_id}"
                          f"/reactions/{emoji}")

    # --- editor blocks + append ---
    def editor_block_create(self, space_id: str, object_id: str, block: dict) -> dict:
        return self._call("POST",
                          f"/v1/spaces/{space_id}/objects/{object_id}/editor/blocks", block)

    def editor_block_patch(self, space_id: str, object_id: str, block_id: str, patch: dict) -> dict:
        return self._call("PATCH",
                          f"/v1/spaces/{space_id}/objects/{object_id}/editor/blocks/{block_id}",
                          patch)

    def editor_block_delete(self, space_id: str, object_id: str, block_id: str) -> dict:
        return self._call("DELETE",
                          f"/v1/spaces/{space_id}/objects/{object_id}/editor/blocks/{block_id}")

    def append_markdown(self, space_id: str, object_id: str, content: str) -> dict:
        return self._call("POST",
                          f"/v1/spaces/{space_id}/objects/{object_id}/editor/markdown/append",
                          {"content": content})

    # --- ui command channel (account-scoped, in-memory) ---
    def ui_command(self, action: str, *, space_id: str | None = None,
                   object_id: str | None = None, source: str | None = None) -> dict:
        body: dict = {"action": action}
        if space_id:
            body["spaceId"] = space_id
        if object_id:
            body["objectId"] = object_id
        if source:
            body["source"] = source
        return self._call("POST", "/v1/ui/commands", body)

    # --- search ---
    def search(self, space_id: str, query: str, *, scopes: list[str] | None = None,
               limit: int | None = None, mode: str | None = None) -> dict:
        body: dict = {"query": query}
        if scopes:
            body["scopes"] = scopes
        if limit:
            body["limit"] = limit
        if mode:
            body["mode"] = mode
        return self._call("POST", f"/v1/spaces/{space_id}/search", body)
