"""any@v1 — the space-data client for the `any` server, built on the
http syscall: JSON transport, error-envelope mapping (AnyError), the
NUL write guard, and typed per-route calls. `space` is always an
explicit argument — cross-space access is normal. Get a bound client
via `client()` (base url from config `any.base_url`) or `client(url)`.
"""

import json


def sanitize_nuls(obj):
    """Strip NUL bytes from strings before any write — anyenc/fastjson
    rejects \\x00 in JSON strings, so we guard at the write boundary.
    Binary-ish HTTP bodies are the realistic source."""
    if isinstance(obj, str):
        return obj.replace("\x00", "�") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: sanitize_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_nuls(v) for v in obj]
    return obj


class AnyError(Exception):
    """A >=400 reply, decoded from the server's error envelope
    `{"error": {"code", "message"}}`."""

    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


class Client:
    def __init__(self, base_url):
        self._base = base_url.rstrip("/")

    def _call(self, verb, path, body=None):
        payload = {"url": self._base + path}
        if body is not None:
            payload["json"] = sanitize_nuls(body)   # write guard
        reply = effect("http." + verb, payload)  # noqa: F821 - guest global
        raw = reply.get("body") or ""
        if reply["status"] >= 400:
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(reply["status"], err.get("code", "unknown"),
                           err.get("message", ""))
        return json.loads(raw) if raw else {}

    # --- objects -------------------------------------------------------------
    def create_object(self, space, body):
        return self._call("post", f"/v1/spaces/{space}/objects", body)

    def query_objects(self, space, **opts):
        """Cross-object query over the per-space objects collection."""
        return self._call("post", f"/v1/spaces/{space}/objects/query",
                          opts).get("records", [])

    def query(self, space, object_id, dataset, **opts):
        """Per-object dataset query (chat_messages, agent_turns, …).
        None-valued opts are dropped so callers can pass through
        optional filter/sort/limit unchecked."""
        body = {"objectId": object_id, "dataset": dataset}
        body.update({k: v for k, v in opts.items() if v is not None})
        return self._call("post", f"/v1/spaces/{space}/query", body).get("records", [])

    def modify(self, space, body):
        return self._call("post", f"/v1/spaces/{space}/modify", body)

    def upsert_record(self, space, object_id, dataset, record_id, value):
        """Write one dataset record (whole-value $set, upsert) — the
        generic path for plain (unregistered) datasets like
        agent_triggers."""
        return self.modify(space, {
            "objectId": object_id, "dataset": dataset,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

    def aggregate(self, space, pipeline):
        return self._call("post", f"/v1/spaces/{space}/objects/aggregate",
                          {"pipeline": pipeline})

    # --- editor markdown (content, NOT markdown — wire landmine) --------------
    def get_markdown(self, space, object_id):
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/editor/markdown")
        return r.get("content", "")

    def put_markdown(self, space, object_id, content):
        return self._call("put",
                          f"/v1/spaces/{space}/objects/{object_id}/editor/markdown",
                          {"content": content})

    # --- types & properties (catalog source) ----------------------------------
    def list_types(self, space):
        return self._call("get", f"/v1/spaces/{space}/types").get("types", [])

    def list_properties(self, space, type_id):
        # [{id, name, xKey, kind}] — the xKey↔propId catalog map.
        r = self._call("get", f"/v1/spaces/{space}/types/{type_id}/properties")
        return r.get("properties", r) if isinstance(r, dict) else r

    def create_type(self, space, body):
        return self._call("post", f"/v1/spaces/{space}/types", body)

    def add_property(self, space, type_id, body):
        return self._call("post", f"/v1/spaces/{space}/types/{type_id}/properties", body)

    # --- agent turns / chunks (server-assigned seq) ----------------------------
    def append_turn(self, space, chat_id, body):
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/turns", body)

    def create_chunk(self, space, chat_id, body):
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/agent/chunks", body)

    # --- chat messages ---------------------------------------------------------
    def chat_send(self, space, chat_id, body):
        return self._call("post",
                          f"/v1/spaces/{space}/objects/{chat_id}/chat/messages", body)

    # --- search & graph ----------------------------------------------------------
    def search(self, space, query, scopes=None, limit=None, mode=None):
        """Index search — the full `{hits, mode, vectorStatus}` envelope."""
        body = {"query": query}
        if scopes:
            body["scopes"] = scopes
        if limit:
            body["limit"] = limit
        if mode:
            body["mode"] = mode
        return self._call("post", f"/v1/spaces/{space}/search", body)

    def backlinks(self, space, object_id):
        """Objects that reference object_id through a links-format
        property. Returns the `backlinks` list unwrapped from the
        envelope — each `{objectId, typeId, propId}` (never null)."""
        r = self._call("get", f"/v1/spaces/{space}/objects/{object_id}/backlinks")
        return r.get("backlinks") or []

    # --- agent memory (write path; reads go through /query on the brain) --------
    def get_brain(self, space):
        """The derived per-space brain object id hosting
        agent_memory_items. `{objectId}` — deterministic, no create race."""
        return self._call("get", f"/v1/spaces/{space}/agent/brain")

    def create_memory(self, space, fields):
        """Create a memory item (category + context required). Server
        resolves the brain object. Returns ModifyResult — recordIds[0]
        is the item id."""
        return self._call("post", f"/v1/spaces/{space}/agent/memory", fields)

    def evolve_memory(self, space, item_id, fields):
        """Evolve a memory item's mutable fields (author only;
        modifiedAt bumped server-side). The route is PATCH-only."""
        return self._call("patch", f"/v1/spaces/{space}/agent/memory/{item_id}", fields)

    def delete_memory(self, space, item_id):
        return self._call("delete", f"/v1/spaces/{space}/agent/memory/{item_id}")


def client(base_url=None):
    """Bind a Client to the server; base url from config `any.base_url`
    unless given explicitly."""
    if base_url is None:
        base_url = effect("config.get", {"key": "any.base_url"})["value"]  # noqa: F821
    return Client(base_url)
