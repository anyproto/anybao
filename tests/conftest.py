"""Integration-test surface for the guest/wire tests.

The unit-level guest tests (`test_*_module.py`, the sweeps, `test_rt_e2e`)
are self-contained. This conftest serves only the `integration`-marked
tests, which hit a REAL `any` server — the wire contract the offline
fakes can't reach. It is deliberately dependency-free: a tiny stdlib
HTTP client (no `anybao`/`anyrt` imports), a live-server probe that
SKIPS when nothing is reachable, and a real-http guest shim.

Server: ANYBAO_TEST_SERVER (default http://127.0.0.1:7009), probed at
/v1/health. Run the suite with `-m integration` and a server up
(`any run --addr 127.0.0.1:7009`).
"""

from __future__ import annotations

import json as _json
import os
import types
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
from kernelenv import kernel_globals  # ts_s / instant / fmt_ts (ADR-019)

DEFAULT_SERVER = "http://127.0.0.1:7009"
PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"


class AnyError(Exception):
    """The `{"error": {code, message}}` envelope, raised on status >= 400."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


def _http_request(method: str, url: str, *, params=None, headers=None,
                  json_body=None, body=None, timeout=None) -> dict:
    """The guest http.* effect, done directly over urllib — the real-http
    twin the guest shim runs on. Returns {status, headers, body:str}."""
    if params:
        from urllib.parse import urlencode
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = _json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout or 30) as resp:
            raw = resp.read()
            return {"status": resp.status,
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": raw.decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return {"status": e.code,
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
                "body": raw.decode(errors="replace")}


class AnyHttp:
    """Minimal typed client for the `any` server. `call` is the whole
    transport (JSON in/out, error envelope → AnyError); the rest are the
    handful of convenience shapes the wire tests use."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return _json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            data = _json.loads(raw) if raw else {}
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AnyError(e.code, err.get("code", "unknown"),
                           err.get("message", "")) from None

    # --- objects ---
    def create_object(self, space: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/objects", body)

    def query_objects(self, space: str, **body) -> list[dict]:
        return self.call("POST", f"/v1/spaces/{space}/objects/query", body).get("records", [])

    def query(self, space: str, object_id: str, dataset: str, **body) -> list[dict]:
        r = self.call("POST", f"/v1/spaces/{space}/query",
                      {"objectId": object_id, "dataset": dataset, **body})
        return r.get("records", [])

    # --- types, parts, datasets (ADR-027 §2) ---
    def create_type(self, space: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/types", body)

    def add_property(self, space: str, type_id: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/types/{type_id}/properties", body)

    def list_types(self, space: str) -> list[dict]:
        return self.call("GET", f"/v1/spaces/{space}/types?includeHidden=true").get("types", [])

    def add_part(self, space: str, type_id: str, draft: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/types/{type_id}/parts", draft)

    def list_datasets(self, space: str, type_id: str) -> list[dict]:
        return self.call("GET", f"/v1/spaces/{space}/types/{type_id}/datasets").get("datasets", [])

    def collection(self, space: str, type_xkey: str, key: str) -> str | None:
        """The collection a type's dataset lives in — read off the
        declaration, never composed."""
        tid = next((t["id"] for t in self.list_types(space) if t.get("xKey") == type_xkey), None)
        if not tid:
            return None
        return next((d.get("collection") for d in self.list_datasets(space, tid)
                     if d.get("key") == key), None)

    # --- catalog + bundles (ADR-027 §1) ---
    def catalog_setup(self, usecase: str, space: str) -> dict:
        return self.call("POST", f"/v1/catalog/{usecase}/setup", {"spaceId": space})

    def general_chat(self, space: str) -> str:
        b = self.catalog_setup("general-chat", space)["bundles"][0]["bundle"]
        assert b.get("derived") is True, b
        return b["rootId"]

    def ensure_bundle(self, space: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/bundles", body)

    def bundle_child(self, space: str, bundle_id: str, seed: str, types=()) -> str:
        enc = bundle_id.replace("/", "%2F").replace(":", "%3A")
        body: dict = {"seed": seed}
        if types:
            body["types"] = list(types)
        return self.call("POST", f"/v1/spaces/{space}/bundles/{enc}/children", body)["objectId"]

    def attach_type(self, space: str, object_id: str, type_id: str) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/properties/{object_id}/attach/{type_id}")

    def modify(self, space: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/modify", body)

    def upsert_record(self, space: str, object_id: str, collection: str,
                      record_id: str, value: dict) -> dict:
        return self.modify(space, {
            "objectId": object_id, "dataset": collection,
            "records": [{"id": record_id, "upsert": True,
                         "ops": [{"type": "$set", "path": "", "value": value}]}]})

    # --- editor ---
    def get_markdown(self, space: str, object_id: str) -> str:
        return self.call("GET", f"/v1/spaces/{space}/objects/{object_id}"
                                "/editor/editor_blocks/markdown").get("content", "")

    def put_markdown(self, space: str, object_id: str, content: str) -> dict:
        return self.call("PUT", f"/v1/spaces/{space}/objects/{object_id}"
                                "/editor/editor_blocks/markdown", {"content": content})

    # --- chat + search ---
    def chat_send(self, space: str, object_id: str, body: dict) -> dict:
        return self.call("POST", f"/v1/spaces/{space}/objects/{object_id}/chat/messages", body)

    def search(self, space: str, query: str, *, scopes=None, limit=None, mode=None) -> dict:
        body: dict = {"query": query}
        if scopes:
            body["scopes"] = list(scopes)
        if limit:
            body["limit"] = limit
        if mode:
            body["mode"] = mode
        return self.call("POST", f"/v1/spaces/{space}/search", body)

    def backlinks(self, space: str, object_id: str) -> dict:
        reply = self.call("GET", f"/v1/spaces/{space}/objects/{object_id}/backlinks")
        return {"object": reply.get("object") or [], "parts": reply.get("parts") or []}


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/v1/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def any_server() -> str:
    url = os.environ.get("ANYBAO_TEST_SERVER", DEFAULT_SERVER)
    if not _reachable(url):
        pytest.skip(f"no any server at {url} (set ANYBAO_TEST_SERVER or `any run --addr`)")
    return url


@pytest.fixture
def client(any_server) -> AnyHttp:
    return AnyHttp(any_server)


@pytest.fixture
def fresh_space(client) -> str:
    """A throwaway space per test (offline-first create — no coordinator)."""
    sp = client.call("POST", "/v1/spaces",
                     {"name": f"it-{uuid.uuid4().hex[:8]}"})
    return sp["id"]


@pytest.fixture
def runtime_values(any_server) -> dict:
    """What `runtime.get` answers in the guest shim: the server url,
    plus `bao.space` once a test provisions a bao space (memory's
    only home, ADR-017 §0) — serve wires the same two keys."""
    return {"any.base_url": any_server}


@pytest.fixture
def bao_space(client, fresh_space, runtime_values) -> str:
    """A throwaway space with what serve provisions before guest code
    runs (ADR-017 §0): the catalog chat and the `bao/v1` bundle the
    guest-owned stores (brain, chat log) derive their children from;
    published to the guest as `bao.space`."""
    client.general_chat(fresh_space)
    client.ensure_bundle(fresh_space, {"id": "bao/v1", "name": "bao", "rootTypes": ["page"]})
    runtime_values["bao.space"] = fresh_space
    return fresh_space


@pytest.fixture
def guest_use(any_server, runtime_values):
    """Guest modules exec'd host-side over a REAL-http effect shim — the
    integration twin of the offline exec-with-fakes technique. `use(spec)`
    loads from programs/ and hits the live server. Self-contained: the
    http effect goes straight through urllib, no project imports."""
    import time as _time

    cache: dict = {}

    def eff(name, payload=None):
        payload = payload or {}
        if name.startswith("http."):
            return _http_request(
                name.split(".")[1].upper(), payload["url"],
                params=payload.get("params"), headers=payload.get("headers"),
                json_body=payload.get("json"), body=payload.get("body"),
                timeout=payload.get("timeout"))
        if name in ("config.get", "runtime.get"):
            if payload["key"] not in runtime_values:
                raise KeyError(f"no runtime value for {payload['key']!r}")
            return {"value": runtime_values[payload["key"]]}
        raise AssertionError(f"unexpected effect in guest shim: {name}")

    def use(spec):
        if spec not in cache:
            g = {"effect": eff, "span": lambda n=None, kind=None: (lambda f: f), "use": use,
                 **kernel_globals(now=int(_time.time()), offset_s=0)}
            # flat <spec>.py or the tool-authoring folder <spec>/program.py
            # — same order as the runtime's local_source_path
            path = PROGRAMS_DIR / f"{spec}.py"
            if not path.exists():
                path = PROGRAMS_DIR / spec / "program.py"
            exec(compile(path.read_text(), f"{spec}.py", "exec"), g)
            mod = types.SimpleNamespace()
            mod.__dict__.update(g)
            cache[spec] = mod
        return cache[spec]

    return use
