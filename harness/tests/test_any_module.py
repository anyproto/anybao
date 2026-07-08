"""programs/any@v1 — the guest space-data client, tested host-side by
exec-ing the module source with a fake `effect` global answering
http.* (json wire replies) and config.get."""

import json
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[2] / "programs" / "any@v1.py").read_text()


def wire(replies=None, status=200, config=None):
    """Fake effect: http.* answers by longest-suffix match on the url
    path; every http call is captured as (VERB, path, json-body)."""
    calls = []

    def fx(name, payload):
        if name == "config.get":
            return {"value": (config or {})[payload["key"]]}
        assert name.startswith("http."), name
        path = payload["url"].removeprefix("http://any")
        calls.append((name.removeprefix("http.").upper(), path, payload.get("json")))
        reply = {}
        for suffix, r in (replies or {}).items():
            if path.endswith(suffix):
                reply = r
                break
        return {"status": status, "headers": {}, "body": json.dumps(reply)}

    fx.calls = calls
    return fx


def load(fx):
    g = {"effect": fx, "span": lambda name: (lambda f: f), "use": None}
    exec(compile(SRC, "any@v1.py", "exec"), g)
    return g


def client(fx, base="http://any"):
    return load(fx)["client"](base)


# --- construction / transport --------------------------------------------------

def test_base_url_comes_from_config():
    fx = wire(config={"any.base_url": "http://any/"})
    c = load(fx)["client"]()          # no explicit url
    c.list_types("s1")
    assert fx.calls == [("GET", "/v1/spaces/s1/types", None)]  # trailing / stripped


def test_error_envelope_maps_to_anyerror():
    fx = wire(status=404, replies={
        "": {"error": {"code": "space.not_found", "message": "no"}}})
    g = load(fx)
    with pytest.raises(g["AnyError"]) as ei:
        g["client"]("http://any").query("s", "o", "d")
    assert ei.value.status == 404 and ei.value.code == "space.not_found"
    assert ei.value.message == "no"


def test_nul_sanitized_on_write():
    fx = wire()
    client(fx).modify("s1", {"body": "before\x00after", "nested": {"x": ["a\x00b"]}})
    _, _, sent = fx.calls[0]
    assert "\x00" not in sent["body"]
    assert "\x00" not in sent["nested"]["x"][0]


def test_sanitize_leaves_clean_strings_identical():
    obj = {"a": "clean", "b": [1, 2]}
    assert load(wire())["sanitize_nuls"](obj) == obj


# --- queries -------------------------------------------------------------------

def test_query_wire_shape_and_unwrap():
    fx = wire(replies={"/query": {"records": [{"id": "1"}]}})
    assert client(fx).query("s", "o", "prop") == [{"id": "1"}]
    assert fx.calls == [("POST", "/v1/spaces/s/query",
                         {"objectId": "o", "dataset": "prop"})]


def test_query_drops_none_opts():
    fx = wire(replies={"/query": {"records": []}})
    client(fx).query("s", "o", "d", filter=None, sort=None, limit=5)
    _, _, body = fx.calls[0]
    assert body == {"objectId": "o", "dataset": "d", "limit": 5}


def test_query_objects_wire_shape_and_unwrap():
    fx = wire(replies={"/objects/query": {"records": [{"id": "r"}]}})
    assert client(fx).query_objects("s1", filter={"id": "x"}, limit=1) == [{"id": "r"}]
    assert fx.calls == [("POST", "/v1/spaces/s1/objects/query",
                         {"filter": {"id": "x"}, "limit": 1})]


def test_aggregate_wraps_pipeline():
    fx = wire()
    client(fx).aggregate("s1", [{"$match": {}}])
    assert fx.calls == [("POST", "/v1/spaces/s1/objects/aggregate",
                         {"pipeline": [{"$match": {}}]})]


# --- writes ---------------------------------------------------------------------

def test_upsert_record_builds_whole_value_set_on_modify():
    fx = wire()
    client(fx).upsert_record("s1", "obj1", "agent_triggers", "t1", {"k": "v"})
    assert fx.calls == [("POST", "/v1/spaces/s1/modify", {
        "objectId": "obj1", "dataset": "agent_triggers",
        "records": [{"id": "t1", "upsert": True,
                     "ops": [{"type": "$set", "path": "", "value": {"k": "v"}}]}]})]


def test_object_type_property_creation_paths():
    fx = wire()
    c = client(fx)
    c.create_object("s1", {"typeId": "t"})
    c.create_type("s1", {"name": "T"})
    c.add_property("s1", "t1", {"name": "P"})
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("POST", "/v1/spaces/s1/objects"),
        ("POST", "/v1/spaces/s1/types"),
        ("POST", "/v1/spaces/s1/types/t1/properties")]


def test_turns_chunks_chat_paths():
    fx = wire()
    c = client(fx)
    c.append_turn("s1", "chat1", {"userText": "hi"})
    c.create_chunk("s1", "chat1", {"level": 1})
    c.chat_send("s1", "chat1", {"text": "yo"})
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("POST", "/v1/spaces/s1/objects/chat1/agent/turns"),
        ("POST", "/v1/spaces/s1/objects/chat1/agent/chunks"),
        ("POST", "/v1/spaces/s1/objects/chat1/chat/messages")]


# --- catalog reads ---------------------------------------------------------------

def test_list_types_and_properties_unwrap():
    fx = wire(replies={"/types": {"types": [{"id": "t1"}]},
                       "/types/t1/properties": {"properties": [{"id": "p1"}]}})
    c = client(fx)
    assert c.list_types("s1") == [{"id": "t1"}]
    assert c.list_properties("s1", "t1") == [{"id": "p1"}]
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1/types"),
        ("GET", "/v1/spaces/s1/types/t1/properties")]


# --- markdown ---------------------------------------------------------------------

def test_markdown_roundtrip_uses_content_key():
    fx = wire(replies={"/editor/markdown": {"content": "# hi"}})
    c = client(fx)
    assert c.get_markdown("s1", "o1") == "# hi"
    c.put_markdown("s1", "o1", "# bye")
    assert fx.calls == [
        ("GET", "/v1/spaces/s1/objects/o1/editor/markdown", None),
        ("PUT", "/v1/spaces/s1/objects/o1/editor/markdown", {"content": "# bye"})]


# --- search / backlinks -------------------------------------------------------------

def test_search_returns_full_envelope_and_optional_fields():
    envelope = {"hits": [], "mode": "hybrid", "vectorStatus": "used"}
    fx = wire(replies={"/search": envelope})
    c = client(fx)
    assert c.search("s1", "q", scopes=["agent"], limit=3, mode="hybrid") == envelope
    c.search("s1", "q")
    assert fx.calls[0][2] == {"query": "q", "scopes": ["agent"], "limit": 3,
                              "mode": "hybrid"}
    assert fx.calls[1][2] == {"query": "q"}   # unset opts stay off the wire


def test_backlinks_unwraps_and_null_degrades_to_empty():
    fx = wire(replies={"/backlinks": {"backlinks": None}})
    assert client(fx).backlinks("s1", "o1") == []
    assert fx.calls == [("GET", "/v1/spaces/s1/objects/o1/backlinks", None)]


# --- agent memory ---------------------------------------------------------------------

def test_memory_verbs_and_paths():
    fx = wire(replies={"/agent/brain": {"objectId": "brain1"}})
    c = client(fx)
    assert c.get_brain("s1") == {"objectId": "brain1"}
    c.create_memory("s1", {"category": "fact", "context": "x"})
    c.evolve_memory("s1", "m1", {"accessCount": 2})
    c.delete_memory("s1", "m1")
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1/agent/brain"),
        ("POST", "/v1/spaces/s1/agent/memory"),
        ("PATCH", "/v1/spaces/s1/agent/memory/m1"),
        ("DELETE", "/v1/spaces/s1/agent/memory/m1")]
