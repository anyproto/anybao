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
from collections.abc import Callable, Iterator
from typing import Any

Transport = Callable[[str, str, dict | None], tuple[int, dict]]
# SSE transport: POST (path, body) → an iterator of raw decoded text
# lines from a text/event-stream response. Injectable for tests.
SSETransport = Callable[[str, dict | None], Iterator[str]]


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


def sse_http_transport(base_url: str) -> SSETransport:
    """POST an SSE subscribe request and stream decoded lines. Kept
    separate from the JSON transport (which is one-shot request/response)
    so the client stays unit-testable with a fake line source."""
    def open_stream(path: str, body: dict | None) -> Iterator[str]:
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        resp = urllib.request.urlopen(req)   # noqa: S310 (localhost)
        try:
            for raw in resp:
                yield raw.decode("utf-8", "replace").rstrip("\n")
        finally:
            resp.close()

    return open_stream


def _parse_sse(lines: Iterator[str]) -> Iterator[dict]:
    """Fold raw SSE lines into `{"event", "data"}` frames. `data:` may
    span multiple lines (joined with \\n); a blank line dispatches the
    frame. `data` is JSON-decoded, falling back to the raw string."""
    event = "message"
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload: Any = _json.loads(raw)
                except ValueError:
                    payload = raw
                yield {"event": event, "data": payload}
            event, data_lines = "message", []
            continue
        if line.startswith(":"):          # SSE comment / heartbeat
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


class AnyClient:
    def __init__(self, transport: Transport, sse_transport: SSETransport | None = None):
        self._send = transport
        self._sse = sse_transport

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

    def upsert_record(self, space_id: str, object_id: str, dataset: str,
                      record_id: str, value: dict) -> dict:
        """Write one dataset record (whole-value $set, upsert) — the
        generic path for plain (unregistered) datasets like agent_triggers."""
        return self.modify(space_id, {
            "objectId": object_id, "dataset": dataset,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

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

    def create_type(self, space_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/types", body)

    def add_property(self, space_id: str, type_id: str, body: dict) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/types/{type_id}/properties", body)

    def aggregate_objects(self, space_id: str, pipeline: list) -> dict:
        return self._call("POST", f"/v1/spaces/{space_id}/objects/aggregate",
                          {"pipeline": pipeline})

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

    def backlinks(self, space_id: str, object_id: str) -> list[dict]:
        """Objects that reference object_id through a links-format
        property. Returns the `backlinks` list unwrapped from the
        envelope — each `{objectId, typeId, propId}` (never null)."""
        reply = self._call(
            "GET", f"/v1/spaces/{space_id}/objects/{object_id}/backlinks")
        return reply.get("backlinks") or []

    # --- agent memory (M5 write path; reads go through /query on the brain) ---
    def get_brain(self, space_id: str) -> dict:
        """The derived per-space brain object id hosting
        agent_memory_items. `{objectId}` — deterministic, no create race."""
        return self._call("GET", f"/v1/spaces/{space_id}/agent/brain")

    def create_memory(self, space_id: str, fields: dict) -> dict:
        """Create a memory item (category + context required). Server
        resolves the brain object. Returns ModifyResult — recordIds[0] is
        the item id."""
        return self._call("POST", f"/v1/spaces/{space_id}/agent/memory", fields)

    def evolve_memory(self, space_id: str, item_id: str, fields: dict) -> dict:
        """Evolve a memory item's mutable fields (author only; modifiedAt
        bumped server-side). accessCount bump on recall rides this path."""
        return self._call(
            "PATCH", f"/v1/spaces/{space_id}/agent/memory/{item_id}", fields)

    def delete_memory(self, space_id: str, item_id: str) -> dict:
        return self._call(
            "DELETE", f"/v1/spaces/{space_id}/agent/memory/{item_id}")

    # --- SSE subscribe (windowed query/subscribe primitive) ---
    def subscribe(self, path: str, body: dict | None = None):
        """Open an SSE stream over a `/query/subscribe`-shaped route and
        yield parsed frames `{"event": str, "data": obj}` in order:
        `ready` → `snapshot` → `changes`* → `closed` (docs/04-events.md).
        Terminal on `closed`. Requires an sse_transport (raises if the
        client was built without one)."""
        if self._sse is None:
            raise RuntimeError("AnyClient has no sse_transport; SSE unavailable")
        if body is not None:
            body = sanitize_nuls(body)
        yield from _parse_sse(self._sse(path, body))

    def subscribe_dataset(self, space_id: str, object_id: str, dataset: str, **opts):
        """Subscribe over one object's dataset (POST
        /v1/spaces/:id/query/subscribe). opts: filter/sort/limit/…"""
        body = {"objectId": object_id, "dataset": dataset, **opts}
        yield from self.subscribe(f"/v1/spaces/{space_id}/query/subscribe", body)
