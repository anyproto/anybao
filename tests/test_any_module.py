"""programs/any@v1 — the guest space-data client, tested host-side by
exec-ing the module source with a fake `effect` global answering
http.* (json wire replies) and config.get."""

import json
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs" / "any@v1"
       / "program.py").read_text()


def wire(replies=None, status=200, config=None):
    """Fake effect: http.* answers by longest-suffix match on the url
    path; every http call is captured as (VERB, path, json-body)."""
    calls = []

    def fx(name, payload):
        if name in ("config.get", "runtime.get"):
            return {"value": (config or {})[payload["key"]]}
        assert name.startswith("http."), name
        url = payload["url"].removeprefix("http://any")
        path, _, query = url.partition("?")
        fx.urls.append(url)
        calls.append((name.removeprefix("http.").upper(), path, payload.get("json")))
        reply = {}
        for suffix, r in (replies or {}).items():
            if path.endswith(suffix):
                reply = r
                break
        return {"status": status, "headers": {}, "body": json.dumps(reply)}

    fx.calls = calls
    fx.urls = []
    return fx


BLOBS = {}   # the fake blob directory behind the kernel's blob.* effects


def _kernel_effect(name, payload, now):
    import base64
    import hashlib
    if name == "blob.put":
        raw = base64.b64decode(payload["data"])
        h = "sha256:" + hashlib.sha256(raw).hexdigest()
        BLOBS[h] = raw
        return {"__blob": h, "bytes": len(raw), "mime": payload["mime"]}
    if name == "blob.read":
        raw = BLOBS[payload["hash"]][payload["offset"]:payload["offset"] + payload["length"]]
        return {"data": base64.b64encode(raw).decode(), "bytes": len(raw)}
    return {"epoch": now, "offset_s": 0}


def load(fx, now=1_787_673_600.0):
    # ts_s / instant / now are kernel globals (ADR-019 §1) — the real
    # implementations, loaded from the guest kernel source
    from kernelenv import load_kernel
    k = load_kernel(effect=lambda n, p: _kernel_effect(n, p, now))
    g = {"effect": fx, "span": lambda name=None, kind=None: (lambda f: f),
         "use": None, "ts_s": k.ts_s, "instant": k.instant, "now": k.now,
         "Blob": k.Blob, "blob": k.blob}
    exec(compile(SRC, "any@v1.py", "exec"), g)
    return g


# The behavior tests below drive the internal _Client with short fixture
# space ids; the flat module functions (the public surface, ADR-010 §8)
# validate spaceConfig shape and are covered by the flat-surface tests
# with an id-shaped space.
def client(fx, base="http://any"):
    return load(fx)["_Client"](base)


# id-shaped (dotted, long, no spaces) — passes the module _sid guard
SID = "bafytestspace0000000000000000.tsuffix"


# --- construction / transport --------------------------------------------------

def test_base_url_comes_from_config():
    fx = wire(config={"any.base_url": "http://any/"})
    g = load(fx)
    g["list_types"](SID)              # no explicit url — _c() reads config
    assert fx.calls == [("GET", f"/v1/spaces/{SID}/types", None)]  # trailing / stripped
    # hidden types (the built-ins, the harness's) always list (ADR-027 §2)
    assert fx.urls == [f"/v1/spaces/{SID}/types?includeHidden=true"]


def test_error_envelope_maps_to_anyerror():
    fx = wire(status=404, replies={
        "": {"error": {"code": "space.not_found", "message": "no"}}})
    g = load(fx)
    with pytest.raises(g["AnyError"]) as ei:
        g["_Client"]("http://any").query("s", "o", "d")
    assert ei.value.status == 404 and ei.value.code == "space.not_found"
    assert ei.value.message == "no"
    assert "hint" not in str(ei.value)          # no hint for unlisted codes


def test_known_codes_carry_recovery_hint_verbatim_message():
    server_msg = 'unknown field "tokensIn"; accepted fields: seq, llm'
    fx = wire(status=400, replies={
        "": {"error": {"code": "request.unknown_field",
                       "message": server_msg}}})
    g = load(fx)
    with pytest.raises(g["AnyError"]) as ei:
        g["_Client"]("http://any").query("s", "o", "d")
    text = str(ei.value)
    assert server_msg in text                   # server message verbatim
    assert "(hint:" in text and "strict body" in text


def test_nul_sanitized_on_write():
    fx = wire()
    client(fx).modify("s1", {"body": "before\x00after", "nested": {"x": ["a\x00b"]}})
    _, _, sent = fx.calls[0]
    assert "\x00" not in sent["body"]
    assert "\x00" not in sent["nested"]["x"][0]


def test_sanitize_leaves_clean_strings_identical():
    obj = {"a": "clean", "b": [1, 2]}
    assert load(wire())["_sanitize_nuls"](obj) == obj


# --- queries -------------------------------------------------------------------

def test_query_wire_shape_and_unwrap():
    # a canonical module collection passes through, no resolution
    fx = wire(replies={"/query": {"records": [{"id": "1"}]}})
    assert client(fx).query("s", "o", "chat_messages") == [{"id": "1"}]
    assert fx.calls == [("POST", "/v1/spaces/s/query",
                         {"objectId": "o", "dataset": "chat_messages"})]


def test_query_drops_none_opts():
    fx = wire(replies={"/query": {"records": []}})
    client(fx).query("s", "o", "editor_blocks", filter=None, sort=None, limit=5)
    _, _, body = fx.calls[0]
    assert body == {"objectId": "o", "dataset": "editor_blocks", "limit": 5}


# A store: the `agent_trigger` type declares `agent_triggers`, whose
# records live in the collection the server minted (ADR-027 §2)
_TRG = {
    "/types": {"types": [{"id": "bafyTRG", "name": "Agent Trigger",
                          "xKey": "agent_trigger", "hidden": True}]},
    "/types/bafyTRG/datasets": {"datasets": [
        {"id": "d1", "key": "agent_triggers", "collection": "bafyTRG_agent_triggers",
         "module": "records", "partId": "p1"}]},
    "/objects/query": {"records": [{"id": "obj1", "any": {"types": ["bafyTRG"]}}]},
}


def test_dataset_keys_resolve_to_the_objects_collection():
    # the model names the store by KEY; the wire carries the collection
    # the declaration reports — never a composed string
    fx = wire(replies={**_TRG, "/query": {"records": [{"id": "t1"}]}})
    c = client(fx)
    assert c.query("s1", "obj1", "agent_triggers") == [{"id": "t1"}]
    q = next(b for v, p, b in fx.calls if p.endswith("/spaces/s1/query"))
    assert q == {"objectId": "obj1", "dataset": "bafyTRG_agent_triggers"}
    # an already-resolved collection passes through
    c.query("s1", "obj1", "bafyTRG_agent_triggers")
    assert fx.calls[-1][2]["dataset"] == "bafyTRG_agent_triggers"
    # a key none of the object's types declare errors with the list
    with pytest.raises(ValueError, match='no type declaring a dataset "ghost".*agent_triggers'):
        c.query("s1", "obj1", "ghost")
    assert c.collection("s1", "agent_trigger", "agent_triggers") == "bafyTRG_agent_triggers"
    assert c.collection("s1", "agent_trigger", "nope") is None


def test_query_objects_wire_shape_and_unwrap():
    fx = wire(replies={"/objects/query": {"records": [{"id": "r"}]}})
    # normalize=False = pure passthrough: exact wire shape, no catalog fetch
    assert client(fx).query_objects("s1", filter={"id": "x"}, limit=1,
                                    normalize=False) == [{"id": "r"}]
    assert fx.calls == [("POST", "/v1/spaces/s1/objects/query",
                         {"filter": {"id": "x"}, "limit": 1})]


def test_aggregate_wraps_pipeline():
    fx = wire()
    client(fx).aggregate("s1", [{"$match": {}}])
    assert fx.calls == [("POST", "/v1/spaces/s1/objects/aggregate",
                         {"pipeline": [{"$match": {}}]})]


# --- writes ---------------------------------------------------------------------

def test_upsert_record_builds_whole_value_set_on_modify():
    fx = wire(replies=_TRG)
    client(fx).upsert_record("s1", "obj1", "agent_triggers", "t1", {"k": "v"})
    assert fx.calls[-1] == ("POST", "/v1/spaces/s1/modify", {
        "objectId": "obj1", "dataset": "bafyTRG_agent_triggers",
        "records": [{"id": "t1", "upsert": True,
                     "ops": [{"type": "$set", "path": "", "value": {"k": "v"}}]}]})


def test_object_type_property_creation_paths():
    fx = wire(replies={"/types": {"types": [{"id": "t1"}], "typeId": "t2"}})
    c = client(fx)
    c.create_object("s1", {})
    c.create_type("s1", {"name": "T"})
    c.add_property("s1", "t1", {"name": "P"})
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("POST", "/v1/spaces/s1/objects"),
        ("GET", "/v1/spaces/s1/types"),          # idempotency probe
        ("POST", "/v1/spaces/s1/types"),
        ("GET", "/v1/spaces/s1/types"),          # add_property xKey resolution
        ("GET", "/v1/spaces/s1/types/t1/properties"),   # xFormat.pos append (ADR-027 §4)
        ("POST", "/v1/spaces/s1/types/t1/properties")]


# --- create_type: the anyHelper composite --------------------------------------

def test_create_type_composite_fans_out_properties():
    fx = wire(replies={
        "/types": {"types": [], "typeId": "t9"},
        "/types/t9/properties": {"properties": [], "propId": "p1"}})
    r = client(fx).create_type("s1", {
        "name": "Comic Book",
        "properties": [{"name": "Author"}, {"name": "year", "kind": "number"}]})
    assert r == {"typeId": "t9", "xKey": "comic_book", "created": True,
                 "addedProps": {"author": "p1", "year": "p1"}}
    posts = [(p, b) for v, p, b in fx.calls if v == "POST"]
    # slugged xKey on the type, no inline properties on the wire
    assert posts[0] == ("/v1/spaces/s1/types",
                        {"name": "Comic Book", "xKey": "comic_book"})
    # each property appended to the display order (xFormat.pos, ADR-027 §4)
    assert posts[1][1] == {"name": "Author", "xKey": "author", "kind": "string",
                           "xFormat": {"pos": "a0"}}
    assert posts[2][1] == {"name": "year", "xKey": "year", "kind": "number",
                           "xFormat": {"pos": "a0"}}   # fake lists no props → a0


def test_create_type_idempotent_adds_only_missing():
    fx = wire(replies={
        "/types": {"types": [{"id": "t9", "name": "Task", "xKey": "task"}]},
        "/types/t9/properties": {
            "properties": [{"id": "p1", "xKey": "status", "kind": "string"}],
            "propId": "p2"}})
    r = client(fx).create_type("s1", {
        "name": "Task",
        "properties": [{"name": "status"}, {"name": "priority"}]})
    assert r == {"typeId": "t9", "xKey": "task", "created": False,
                 "addedProps": {"priority": "p2"}}
    posts = [p for v, p, _ in fx.calls if v == "POST"]
    assert posts == ["/v1/spaces/s1/types/t9/properties"]  # no type POST, one prop


def test_create_type_builtin_handle_errors():
    fx = wire(replies={
        "/types": {"types": [{"id": "type", "name": "Type", "xKey": "type"}]}})
    with pytest.raises(ValueError, match="builtin"):
        client(fx).create_type("s1", {"name": "Type"})
    with pytest.raises(ValueError, match="builtin"):
        client(fx).create_type("s1", {"name": "My Meta", "xKey": "type"})
    assert [v for v, _, _ in fx.calls if v == "POST"] == []


def test_create_object_rejects_synthetic_types():
    fx = wire()
    with pytest.raises(ValueError, match="synthetic"):
        client(fx).create_object("s1", {"types": ["type"]})
    with pytest.raises(ValueError, match="synthetic"):
        client(fx).create_object("s1", {"types": ["page", "spaceIndex"]})
    assert fx.calls == []


def test_add_property_defaults_xkey_and_kind():
    fx = wire(replies={"/types": {"types": [{"id": "t1"}]},
                       "/types/t1/properties": {"propId": "p1"}})
    client(fx).add_property("s1", "t1", {"name": "Due Date"})
    assert fx.calls[-1][2] == {"name": "Due Date", "xKey": "due_date",
                               "kind": "string", "xFormat": {"pos": "a0"}}


# --- xKey normalization (ADR-006 §6) -------------------------------------------

# A catalog with one user type `task` (CID id) + the hidden built-in
# `page` (id == xKey, builtIn). Builtin groups carry their own property
# catalogs (mirrors the server): filter paths under any resolve against
# them (A19).
_ANY_PROPS = {"properties": [
    {"id": "id", "kind": "string", "scope": "derived"},
    {"id": "createdAt", "kind": "datetime", "scope": "derived"},
    {"id": "name", "name": "Name", "kind": "string", "scope": "synced"},
    {"id": "description", "kind": "string", "scope": "synced"},
    {"id": "types", "kind": "array", "scope": "synced"}]}
_CAT = {
    "/types": {"types": [
        {"id": "bafyTASK", "name": "Task", "xKey": "task"},
        {"id": "page", "name": "page", "xKey": "page", "hidden": True, "builtIn": True}]},
    "/types/any/properties": _ANY_PROPS,
    "/types/bafyTASK/properties": {"properties": [
        {"id": "bafySTATUS", "name": "Status", "xKey": "status"},
        {"id": "bafyPRIO", "name": "Priority", "xKey": "priority"}]}}


def test_query_objects_normalizes_user_groups_keeps_builtins():
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "any": {"name": "Ship", "types": ["bafyTASK", "page"]},
         "bafyTASK": {"bafySTATUS": "open", "bafyPRIO": 3}}]}})
    [rec] = client(fx).query_objects("s1", filter={"any.types": "task"})
    # user group + its props rekeyed to xKeys; any.types VALUES too;
    # builtins (page, id) verbatim
    assert rec == {"id": "o1", "any": {"name": "Ship", "types": ["task", "page"]},
                   "task": {"status": "open", "priority": 3}}


def test_query_objects_resolves_filter_and_sort_xkey_paths():
    fx = wire(replies={**_CAT, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"any.types": "task",
                                           "task.status": "open"},
                             sort=["-task.priority"])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    # any.types VALUE + dotted xKey paths resolved to server ids; builtin passthrough
    assert body["filter"] == {"any.types": "bafyTASK",
                              "bafyTASK.bafySTATUS": "open"}
    assert body["sort"] == ["-bafyTASK.bafyPRIO"]


def test_create_object_resolves_types_and_property_groups():
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    client(fx).create_object("s1", {
        "types": ["task"],
        "initialProperties": {"any": {"name": "Ship it"},
                              "task": {"status": "open", "priority": 3}}})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body == {"types": ["bafyTASK"], "initialProperties": {
        "any": {"name": "Ship it"},                    # reserved: literal
        "bafyTASK": {"bafySTATUS": "open", "bafyPRIO": 3}}}


def test_create_object_unknown_property_raises_never_drops():
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    with pytest.raises(ValueError, match='unknown property "nope" on type "task"'):
        client(fx).create_object("s1", {
            "types": ["task"], "initialProperties": {"task": {"nope": 1}}})
    # nothing was written — the object POST never fired
    assert not any(p == "/v1/spaces/s1/objects" for v, p, _ in fx.calls)


def test_create_object_unknown_type_lists_available():
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match='type "ghost" doesn.t exist'):
        client(fx).create_object("s1", {"types": ["ghost"]})


def test_space_argument_must_be_a_string():
    # a list_spaces() row (or the list itself) passed as `space` must
    # fail at the boundary, not frames deep as an unhashable dict key
    fx = wire(replies=_CAT)
    with pytest.raises(TypeError, match="space must be a space id string"):
        client(fx).query_objects(["s1"], filter={"any.types": "task"})


_CHAT_SETUP = {"usecase": "general-chat", "bundles": [{
    "usecase": "general-chat", "id": "system:general-chat/v1",
    "bundle": {"id": "system:general-chat/v1", "rootId": "chat9",
               "roots": ["chat9"], "derived": True},
    "installed": False, "typeId": "chat9",
    "miniapp": {"bundle": "system:general-chat/v1"}}]}


def test_get_space_and_general_chat():
    # the chat is the catalog's general chat (ADR-027 §1): one setup
    # call, adopt or install alike, cached per run; the bundle id's
    # slash is percent-encoded in registry paths
    fx = wire(replies={"/spaces/s1": {"id": "s1"},
                       "/catalog/general-chat/setup": _CHAT_SETUP,
                       "/bundles/bao%2Fv1": {
                           "bundle": {"id": "bao/v1", "rootId": "root1",
                                      "roots": ["root1"]},
                           "synced": True}})
    c = client(fx)
    assert c.get_space("s1")["id"] == "s1"
    assert c.general_chat("s1") == "chat9"
    assert c.general_chat("s1") == "chat9"
    # the locked-read envelope {bundle, synced} is unwrapped to the row
    row = c.get_bundle("s1", "bao/v1")
    assert row["rootId"] == "root1" and row["synced"] is True
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1"),
        ("POST", "/v1/catalog/general-chat/setup"),
        ("GET", "/v1/spaces/s1/bundles/bao%2Fv1")]
    assert fx.calls[1][2] == {"spaceId": "s1"}


def test_general_chat_refuses_a_non_derived_root():
    fx = wire(replies={"/catalog/general-chat/setup": {"bundles": [{
        "id": "system:general-chat/v1",
        "bundle": {"id": "system:general-chat/v1", "rootId": "old-root"}}]}})
    g = load(fx)
    with pytest.raises(g["AnyError"], match="non-derived object old-root"):
        g["_Client"]("http://any").general_chat("s1")


def test_delete_object_wire_path():
    fx = wire()
    assert client(fx).delete_object("s1", "o1") == {}
    assert fx.calls == [("DELETE", "/v1/spaces/s1/objects/o1", None)]


def test_create_object_routes_top_level_name_and_description():
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    client(fx).create_object("s1", {"types": ["task"], "name": "Dune",
                                    "description": "a note"})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body["initialProperties"]["any"] == {"name": "Dune",
                                                "description": "a note"}
    assert "name" not in body and "description" not in body


def test_create_object_markdown_writes_the_body_after_create():
    # one call creates a page: the body lives on the built-in `page`
    # (added to `types` — no write attaches a type, ADR-027 §3) and
    # markdown (alias `body`) rides as a put_markdown after the create
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"},
                       "/editor/editor_blocks/markdown": {}})
    client(fx).create_object("s1", {"types": ["task"], "name": "Dune",
                                    "markdown": "# Dune\n\nsand"})
    paths = [(v, p) for v, p, _ in fx.calls]
    i_create = paths.index(("POST", "/v1/spaces/s1/objects"))
    i_md = paths.index(("PUT", "/v1/spaces/s1/objects/o9/editor/editor_blocks/markdown"))
    assert i_create < i_md
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert "markdown" not in body
    assert body["types"] == ["bafyTASK", "page"]
    md = next(b for v, p, b in fx.calls if p.endswith("/editor/editor_blocks/markdown"))
    assert md["content"] == "# Dune\n\nsand"
    # no attach round-trip: the create already carried `page`
    assert not any("/attach/" in p for _, p, _ in fx.calls)


def test_create_object_parent_places_it_in_the_wiki_tree():
    # parent= → the catalog's wiki usecase (set up once per space per
    # run), the wiki type on the object, parentId + a position after
    # the last sibling (ADR-027 §3)
    wiki = {"usecase": "wiki", "bundles": [{
        "id": "system:wiki/v1", "installed": False, "typeId": "bafyWIKI",
        "bundle": {"id": "system:wiki/v1", "rootId": "bafyWIKI"},
        "properties": {"parentId": "pPAR", "pos": "pPOS", "folder": "pFOL"}}]}
    replies = {**_CAT, "/objects": {"objectId": "o9"},
               "/catalog/wiki/setup": wiki,
               "/objects/query": {"records": [{"id": "sib", "bafyWIKI": {"pPOS": "a3"}}]}}
    fx = wire(replies=replies)
    c = client(fx)
    c.create_object("s1", {"types": ["task"], "name": "Dune"}, parent="", folder=True)
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body["types"] == ["bafyTASK", "bafyWIKI"]
    assert body["initialProperties"]["bafyWIKI"] == {"pPAR": "", "pPOS": "a4", "pFOL": True}
    sib = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert sib == {"filter": {"bafyWIKI.pPAR": ""}, "sort": ["-bafyWIKI.pPOS"], "limit": 1}
    # the setup ran once; a second placed object reuses it
    c.create_object("s1", {"name": "Heat"}, parent="o9")
    assert [p for _, p, _ in fx.calls].count("/v1/catalog/wiki/setup") == 1
    # move_object attaches the type when missing and re-places
    fx.calls.clear()
    c.move_object("s1", "o1", "o9")
    verbs = [(v, p.split("/s1/")[1]) for v, p, _ in fx.calls if v != "GET"]
    assert ("POST", "properties/o1/attach/bafyWIKI") in verbs
    assert ("POST", "properties/o1/set/bafyWIKI") in verbs


def test_create_object_unknown_top_level_key_raises_never_posts():
    # the wire accepts only types/initialProperties and rejects the
    # rest — the client refuses first instead of losing intent (and
    # `nav` is no key at all: the tree is `parent=`)
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match="unknown top-level key"):
        client(fx).create_object("s1", {"types": ["task"],
                                        "any": {"name": "x"}})
    with pytest.raises(ValueError, match="parent= places"):
        client(fx).create_object("s1", {"nav": {"parentId": ""}})
    assert not any(p == "/v1/spaces/s1/objects" for v, p, _ in fx.calls)


def test_query_objects_unknown_opt_raises_never_queries():
    # a typo'd opt (filters=) is an unvisited key server-side: the
    # query would silently match EVERY object in the space
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match="unknown option"):
        client(fx).query_objects("s1", filters={"any.types": "task"})
    assert not any(p.endswith("/objects/query") for v, p, _ in fx.calls)


def test_query_filters_error_on_unknown_keys_never_silent_empty():
    # the store answers a typo'd key with a silent empty set — the
    # resolution layer must refuse to forward what it can't resolve
    # (ADR-006 §6, reads like writes)
    fx = wire(replies=_CAT)
    c = client(fx)
    with pytest.raises(ValueError, match='type "unicorn" doesn.t exist'):
        c.query_objects("s1", filter={"any.types": "unicorn"})
    with pytest.raises(ValueError, match='type "unicorn" doesn.t exist'):
        c.query_objects("s1", filter={"any.types": {"$in": ["task", "unicorn"]}})
    with pytest.raises(ValueError, match='unknown property "nope" on type "task"'):
        c.query_objects("s1", filter={"task.nope": 1})
    with pytest.raises(ValueError, match='type "bookz" doesn.t exist'):
        c.query_objects("s1", filter={"bookz.rating": {"$gte": 5}})
    with pytest.raises(ValueError, match='unknown property "nope"'):
        c.query_objects("s1", sort=["-task.nope"])
    # nothing reached the wire beyond catalog reads
    assert not any(p.endswith("/objects/query") for _, p, _ in fx.calls)
    # data-dependent empties are untouched: resolvable key, no matches
    assert c.query_objects("s1", filter={"task.status": "open"}) == []


def test_update_object_writes_name_markdown_and_prop_groups():
    # o1 carries no body-declaring type yet: the body write attaches
    # `page` first (no write attaches a type server-side, ADR-027 §3)
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "any": {"types": ["bafyTASK"]}}]}})
    r = client(fx).update_object("s1", "o1", {
        "name": "Renamed", "description": "now with mangoes",
        "markdown": "# body", "task": {"status": "done"}})
    assert r == {"objectId": "o1"}
    posts = [(p, b) for v, p, b in fx.calls if v in ("POST", "PUT")]
    i_attach = posts.index(("/v1/spaces/s1/properties/o1/attach/page", None))
    i_md = posts.index(("/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown",
                        {"content": "# body"}))
    assert i_attach < i_md
    # name/description -> set/any patch (parity with create_object);
    # property group -> set/<typeId> patch
    assert ("/v1/spaces/s1/properties/o1/set/any",
            {"patch": {"name": "Renamed", "description": "now with mangoes"}}) in posts
    assert ("/v1/spaces/s1/properties/o1/set/bafyTASK",
            {"patch": {"bafySTATUS": "done"}}) in posts


def test_normalize_false_returns_raw_id_keyed_groups():
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "bafyTASK": {"bafySTATUS": "open"}}]}})
    [rec] = client(fx).query_objects("s1", normalize=False)
    assert rec == {"id": "o1", "bafyTASK": {"bafySTATUS": "open"}}


def test_normalize_strips_ver_noise_raw_keeps_it():
    # B5: _ver version vectors are tokens the model can't use
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "_ver": {"id": "!!$5"},
         "bafyTASK": {"bafySTATUS": "open"}}]}})
    [rec] = client(fx).query_objects("s1")
    assert "_ver" not in rec
    assert rec["task"] == {"status": "open"}
    [raw] = client(fx).query_objects("s1", normalize=False)
    assert raw["_ver"] == {"id": "!!$5"}


def test_catalog_refreshes_once_on_unknown_type_miss():
    # First /types reply lacks `task`; resolution refreshes the catalog and
    # the wire (stateful) then returns it. Proves refresh-on-miss.
    seen = {"n": 0}

    def fx(name, payload):
        if name in ("config.get", "runtime.get"):
            return {"value": None}
        path = payload["url"].removeprefix("http://any").partition("?")[0]
        if path.endswith("/types"):
            seen["n"] += 1
            types = [] if seen["n"] == 1 else [
                {"id": "bafyTASK", "name": "Task", "xKey": "task"}]
            return {"status": 200, "headers": {}, "body": json.dumps({"types": types})}
        if path.endswith("/objects"):
            return {"status": 200, "headers": {}, "body": json.dumps({"objectId": "o9"})}
        return {"status": 200, "headers": {}, "body": "{}"}

    c = load(fx)["_Client"]("http://any")
    c.create_object("s1", {"types": ["task"]})   # miss then hit
    assert seen["n"] == 2


def test_create_dataset_patches_drifted_multifield_text():
    # SYN-179: a draft whose search.text names more fields than the
    # existing def is drift — ensure PATCHes the array leaf back
    fx = wire(replies={
        "/types/mb/datasets": {"datasets": [
            {"id": "d1", "key": "email_messages", "collection": "mb_email_messages",
             "search": {"title": "subject", "text": "body",
                        "scope": "email"}}]},
        "/types": {"types": [{"id": "mb", "xKey": "mailbox"}]},
    })
    r = client(fx).create_dataset("s1", "mailbox", {
        "key": "email_messages",
        "search": {"title": "subject", "text": ["body", "notes"],
                   "scope": "email"}})
    assert r == {"datasetDefId": "d1", "collection": "mb_email_messages",
                 "created": False, "patched": ["search.text"]}
    verb, path, body = next(c for c in fx.calls if c[0] == "PATCH")
    assert path.endswith("/types/mb/datasets/d1")
    assert body == {"set": {"search.text": ["body", "notes"]}}


def test_create_dataset_single_element_text_array_is_not_drift():
    # The server stores a one-key array as the bare string; a draft
    # saying ["body"] against a stored "body" must NOT patch
    fx = wire(replies={
        "/types/mb/datasets": {"datasets": [
            {"id": "d1", "key": "email_messages", "collection": "mb_email_messages",
             "search": {"title": "subject", "text": "body",
                        "scope": "email"}}]},
        "/types": {"types": [{"id": "mb", "xKey": "mailbox"}]},
    })
    r = client(fx).create_dataset("s1", "mailbox", {
        "key": "email_messages",
        "search": {"title": "subject", "text": ["body"],
                   "scope": "email"}})
    assert r == {"datasetDefId": "d1", "collection": "mb_email_messages",
                 "created": False}
    assert not [c for c in fx.calls if c[0] == "PATCH"]


def test_create_dataset_declares_one_part_per_store():
    # a missing store is declared as a part with the dataset inline
    # under the same key; the collection is read back off the listing
    # (ADR-027 §2) — never composed
    seen = {"n": 0}

    def fx(name, payload):
        if name in ("config.get", "runtime.get"):
            return {"value": None}
        path = payload["url"].removeprefix("http://any").partition("?")[0]
        fx.calls.append((name.removeprefix("http.").upper(), path, payload.get("json")))
        if path.endswith("/types"):
            reply = {"types": [{"id": "mb", "xKey": "mailbox"}]}
        elif path.endswith("/types/mb/datasets"):
            seen["n"] += 1
            reply = {"datasets": [] if seen["n"] == 1 else [
                {"id": "d9", "key": "email_messages", "collection": "mb_email_messages",
                 "module": "records", "partId": "prt1"}]}
        elif path.endswith("/parts"):
            reply = {"partId": "prt1"}
        else:
            reply = {}
        return {"status": 200, "headers": {}, "body": json.dumps(reply)}
    fx.calls = []
    c = load(fx)["_Client"]("http://any")
    draft = {"key": "email_messages", "idRule": "user",
             "fields": [{"key": "subject", "kind": "string"}]}
    assert c.create_dataset("s1", "mailbox", draft) == {
        "datasetDefId": "d9", "collection": "mb_email_messages", "created": True}
    part = next(b for v, p, b in fx.calls if p.endswith("/parts"))
    assert part == {"key": "email_messages", "datasets": [draft]}
    with pytest.raises(ValueError, match='"name" is not a dataset field'):
        c.create_dataset("s1", "mailbox", {"name": "x"})


def test_turns_chunks_chat_paths():
    # ADR-017: turns/chunks land on the chat's log child (bao/log/v1 of
    # the chat's bundle) via upsert with a client-assigned seq; the
    # agent_log store is ensured lazily (type + datasets pre-exist in
    # this fixture, search leaves matching so nothing is patched)
    fx = wire(replies={
        "/types/lg/datasets": {"datasets": [
            {"id": "d1", "key": "agent_turns", "collection": "lg_agent_turns",
             "search": {"title": "userText", "text": "searchText",
                        "scope": "history"}},
            {"id": "d2", "key": "agent_chunks", "collection": "lg_agent_chunks",
             "search": {"text": "summary", "scope": "history"}}]},
        "/types": {"types": [{"id": "lg", "xKey": "agent_log"}]},
        "/children": {"objectId": "log1"},
        "/bundles": {"bundles": [{"id": "system:general-chat/v1",
                                  "rootId": "chat1", "derived": True}]},
        # the highest id ever written is a TOMBSTONE (content wiped, no
        # seq) — the allocator must still count past it (ADR-017 §2)
        "/query": {"records": [{"id": "00000004",
                                "_deletedAt": {"$date": "2026-09-01T00:00:00Z"}}]},
        "/upsert": {"created": 1, "updated": 0, "skipped": 0},
    })
    c = client(fx)
    r = c.append_turn("s1", "chat1", {"userText": "hi", "replies": ["yo"]})
    assert r == {"recordIds": ["00000005"], "seq": 5}
    # the log child derives under the catalog chat's bundle; the store
    # is addressed by its collection on the wire (ADR-027 §1/§2)
    child = next(p for v, p, b in fx.calls if p.endswith("/children"))
    assert child == "/v1/spaces/s1/bundles/system%3Ageneral-chat%2Fv1/children"
    probe = next(b for v, p, b in fx.calls if p.endswith("/query")
                 and b.get("dataset") == "lg_agent_turns")
    assert probe == {"objectId": "log1", "dataset": "lg_agent_turns",
                     "includeDeleted": True, "sort": ["-id"], "limit": 1}
    c.create_chunk("s1", "chat1", {"level": 1})
    c.chat_send("s1", "chat1", {"text": "yo"})
    turn_up = next(b for v, p, b in fx.calls
                   if p.endswith("/upsert")
                   and b["dataset"] == "lg_agent_turns")
    assert turn_up["objectId"] == "log1"
    rec = turn_up["records"][0]
    assert rec["id"] == "00000005"
    assert rec["fields"]["seq"] == 5
    assert rec["fields"]["searchText"] == "hi yo"
    chunk_up = next(b for v, p, b in fx.calls
                    if p.endswith("/upsert")
                    and b["dataset"] == "lg_agent_chunks")
    assert chunk_up["records"][0]["fields"]["level"] == 1
    assert fx.calls[-1][1] == "/v1/spaces/s1/objects/chat1/chat/messages"


# --- catalog reads ---------------------------------------------------------------

def test_list_types_and_properties_unwrap():
    fx = wire(replies={"/types": {"types": [{"id": "t1"}]},
                       "/types/t1/properties": {"properties": [{"id": "p1"}]}})
    c = client(fx)
    assert c.list_types("s1") == [{"id": "t1"}]
    assert c.list_properties("s1", "t1") == [{"id": "p1", "handle": "p1"}]
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1/types"),
        ("GET", "/v1/spaces/s1/types"),   # list_properties type resolution
        ("GET", "/v1/spaces/s1/types/t1/properties")]


def test_list_properties_takes_xkey_and_errors_on_unknown():
    fx = wire(replies=_CAT)
    c = client(fx)
    # xKey resolves to the CID route — the agent never needs the id
    rows = c.list_properties("s1", "task")
    # display order: no xFormat.pos → by name; handle = the slug xKey
    assert [(r["handle"], r["xKey"]) for r in rows] == [
        ("priority", "priority"), ("status", "status")]
    assert any(p.endswith("/types/bafyTASK/properties") for _, p, _ in fx.calls)
    # unknown key errors with the catalog (server would answer 200 [])
    with pytest.raises(ValueError, match='type "ghost" doesn.t exist'):
        c.list_properties("s1", "ghost")


def test_add_property_takes_xkey():
    fx = wire(replies={**_CAT, "/types/bafyTASK/properties": {"propId": "p9"}})
    client(fx).add_property("s1", "task", {"name": "Due"})
    verb, path, _ = fx.calls[-1]
    assert (verb, path) == ("POST", "/v1/spaces/s1/types/bafyTASK/properties")


def test_add_property_derives_kind_from_the_slug():
    # any never derives kind from the descriptor; the client does, so a
    # bare slug declares the right storage kind (ADR-027 §4)
    fx = wire(replies={**_CAT, "/types/bafyTASK/properties": {"propId": "p9"}})
    c = client(fx)
    for slug, kind in (("date", "datetime"), ("choice", "array"), ("relation", "array"),
                       ("checkbox", "boolean"), ("currency", "number"),
                       ("money", "object"), ("url", "string")):
        c.add_property("s1", "task", {"name": "P " + slug, "xFormat": {"type": slug}})
        body = fx.calls[-1][2]
        assert body["kind"] == kind, slug
        assert body["xFormat"]["type"] == slug and body["xFormat"]["pos"] == "a0"
    # the old spellings are refused, never silently dropped
    with pytest.raises(ValueError, match='no "format" or "xKind"'):
        c.add_property("s1", "task", {"name": "X", "format": {"type": "date"}})
    with pytest.raises(ValueError, match='meta takes only "index"'):
        c.add_property("s1", "task", {"name": "X", "meta": {"pos": "a0"}})


def test_aggregate_speaks_xkeys_in_records():
    fx = wire(replies={**_CAT, "/objects/aggregate": {"records": [
        {"id": ["bafyTASK", "page"], "count": 2},
        {"id": ["miniapp"], "count": 1}]}})
    r = client(fx).aggregate("s1", [{"$group": {"_id": "$any.types",
                                                "count": {"$sum": 1}}}])
    assert r["records"] == [{"id": ["task", "page"], "count": 2},
                            {"id": ["miniapp"], "count": 1}]


def test_aggregate_resolves_xkey_field_refs_in_pipeline():
    fx = wire(replies={**_CAT, "/objects/aggregate": {"records": []}})
    client(fx).aggregate("s1", [
        {"$match": {"any.types": "task", "task.priority": {"$gte": 2}}},
        {"$group": {"_id": "$task.status",
                    "avg": {"$avg": "$task.priority"},
                    "n": {"$sum": 1}}},
        {"$sort": {"task.priority": -1, "n": 1}}])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/aggregate"))
    assert body["pipeline"] == [
        {"$match": {"any.types": "bafyTASK",
                    "bafyTASK.bafyPRIO": {"$gte": 2}}},
        {"$group": {"_id": "$bafyTASK.bafySTATUS",
                    "avg": {"$avg": "$bafyTASK.bafyPRIO"},
                    "n": {"$sum": 1}}},   # int + non-ref strings untouched
        {"$sort": {"bafyTASK.bafyPRIO": -1, "n": 1}}]


def test_aggregate_pipeline_unknown_ref_errors_literals_pass():
    fx = wire(replies=_CAT)
    c = client(fx)
    with pytest.raises(ValueError, match='unknown property "nope" on type "task"'):
        c.aggregate("s1", [{"$group": {"_id": "$task.nope"}}])
    with pytest.raises(ValueError, match='type "ghost" doesn.t exist'):
        c.aggregate("s1", [{"$match": {"ghost.x": 1}}])
    # value-position literals are never resolved ($literal ambiguity):
    # a $match VALUE that merely looks dotted ships verbatim
    fx2 = wire(replies={**_CAT, "/objects/aggregate": {"records": []}})
    client(fx2).aggregate("s1", [{"$match": {"any.name": "v1.2 release"}}])
    body = next(b for v, p, b in fx2.calls
                if p.endswith("/objects/aggregate"))
    assert body["pipeline"] == [{"$match": {"any.name": "v1.2 release"}}]


def test_backlinks_speaks_xkeys():
    # the link index's edges (ADR-027 §5): a relation value names its
    # property as "type.prop"; a block names its collection and record
    fx = wire(replies={**_CAT, "/objects/o1/backlinks": {
        "object": [
            {"source": {"spaceId": "s1", "objectId": "src", "dataset": "prop",
                        "recordId": "bafySTATUS", "typeId": "bafyTASK"},
             "kind": "relation", "target": {"uri": "any://o/s1/o1"}},
            {"source": {"spaceId": "s1", "objectId": "pg", "dataset": "editor_blocks",
                        "recordId": "blk1"},
             "kind": "link", "target": {"uri": "any://o/s1/o1"}}],
        "parts": [], "truncated": True}})
    assert client(fx).backlinks("s1", "o1") == {
        "object": [
            {"objectId": "src", "kind": "relation", "target": "any://o/s1/o1",
             "prop": "task.status"},
            {"objectId": "pg", "kind": "link", "target": "any://o/s1/o1",
             "dataset": "editor_blocks", "key": "editor_blocks", "recordId": "blk1"}],
        "parts": [], "truncated": True}


def test_links_and_account_wide_backlinks_paths():
    fx = wire(replies={"/objects/o1/links": {"links": [
        {"source": {"spaceId": "s1", "objectId": "o1", "dataset": "chat_messages",
                    "recordId": "m1"}, "kind": "link", "target": {"uri": "any://o/s1/x"}}]},
        "/v1/backlinks": {"spaces": [{"spaceId": "s2", "object": [], "parts": []}]}})
    c = client(fx)
    assert c.links("s1", "o1") == [{"objectId": "o1", "kind": "link",
                                    "target": "any://o/s1/x", "dataset": "chat_messages",
                                    "key": "chat_messages", "recordId": "m1"}]
    assert c.backlinks_everywhere("any://o/s1/o1") == [
        {"spaceId": "s2", "object": [], "parts": []}]
    assert fx.urls[-1] == "/v1/backlinks?target=any%3A%2F%2Fo%2Fs1%2Fo1"


# --- markdown ---------------------------------------------------------------------

def test_edit_markdown_wire_shape():
    fx = wire(replies={"/editor/editor_blocks/markdown": {"updated": 1, "unchanged": 4}})
    r = client(fx).edit_markdown("s1", "o1", [
        {"oldText": "- [ ] Buy milk", "newText": "- [x] Buy milk"}])
    verb, path, body = fx.calls[-1]
    assert (verb, path) == ("PATCH",
                            "/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown")
    assert body == {"edits": [{"oldText": "- [ ] Buy milk",
                               "newText": "- [x] Buy milk"}]}
    assert r == {"updated": 1, "unchanged": 4}


def test_markdown_roundtrip_uses_content_key():
    # o1 already carries `page`: no attach, the routes name the shared
    # editor collection (ADR-027 §3)
    fx = wire(replies={"/editor/editor_blocks/markdown": {"content": "# hi"},
                       "/objects/query": {"records": [
                           {"id": "o1", "any": {"types": ["page"]}}]}})
    c = client(fx)
    assert c.get_markdown("s1", "o1") == "# hi"
    c.put_markdown("s1", "o1", "# bye")
    c.append_markdown("s1", "o1", "\n## more")
    assert [x for x in fx.calls if not x[1].endswith("/objects/query")] == [
        ("GET", "/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown", None),
        ("PUT", "/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown",
         {"content": "# bye"}),
        ("POST", "/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown/append",
         {"content": "\n## more"})]
    # one object read decided both writes needed no attach
    assert [p for _, p, _ in fx.calls].count("/v1/spaces/s1/objects/query") == 1


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


def test_search_enriches_hits_with_title_and_type():
    fx = wire(replies={
        "/search": {"hits": [
            {"objectId": "o1", "data": "game", "dataset": "prop", "score": 0.3},
            {"objectId": "o2", "data": "soul", "dataset": "editor_blocks",
             "score": 0.1}], "mode": "hybrid"},
        "/objects/query": {"records": [
            {"id": "o1", "any": {"name": "Game", "types": ["page", "ty_game"]}},
            {"id": "o2", "any": {"name": "_soul", "types": ["agent_skill", "page"]}}]},
        "/types": {"types": [{"id": "ty_game", "name": "Game"},
                             {"id": "agent_skill", "name": "Agent Skill"}]}})
    hits = client(fx).search("s1", "game")["hits"]
    # the hidden built-ins never win the primary type
    assert (hits[0]["title"], hits[0]["type"]) == ("Game", "Game")
    assert (hits[1]["title"], hits[1]["type"]) == ("_soul", "Agent Skill")
    # every hit names its store key next to the collection
    assert hits[1]["key"] == "editor_blocks"
    # one batch resolution over both ids, not a lookup per hit
    resolves = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert len(resolves) == 1
    assert set(resolves[0]["filter"]["id"]["$in"]) == {"o1", "o2"}


def test_search_prop_hits_gain_type_prop_xkey():
    fx = wire(replies={
        "/search": {"hits": [
            {"objectId": "o1", "data": "open", "dataset": "prop",
             "recordId": "bafySTATUS", "score": 0.3},
            {"objectId": "o1", "data": "Ship", "dataset": "prop",
             "recordId": "name", "score": 0.2}], "mode": "hybrid"},
        "/objects/query": {"records": [
            {"id": "o1", "any": {"name": "Ship",
                                 "types": ["bafyTASK", "page"]}}]},
        **_CAT})
    hits = client(fx).search("s1", "open")["hits"]
    assert hits[0]["prop"] == "task.status"   # propId resolved via the catalog
    assert "prop" not in hits[1]              # builtin name recordId untouched


def test_search_enrich_false_skips_the_extra_query():
    fx = wire(replies={"/search": {"hits": [
        {"objectId": "o1", "dataset": "bafyreibrain0000000000000_agent_memory_items"}]}})
    hits = client(fx).search("s1", "q", enrich=False)["hits"]
    assert [p for v, p, _ in fx.calls] == ["/v1/spaces/s1/search"]
    # a namespaced collection reads back as its store key
    assert hits[0]["key"] == "agent_memory_items"


def test_backlinks_unwraps_and_null_degrades_to_empty():
    fx = wire(replies={"/backlinks": {"object": None, "parts": None}})
    assert client(fx).backlinks("s1", "o1") == {"object": [], "parts": []}
    assert fx.calls == [("GET", "/v1/spaces/s1/objects/o1/backlinks", None)]


# --- agent memory ---------------------------------------------------------------------

def _brain_wire():
    # agent_brain store pre-exists (type + datasets with matching
    # search leaves), brain child derives to brain1
    return {
        "/types/br/datasets": {"datasets": [
            {"id": "d1", "key": "agent_memory_items",
             "collection": "br_agent_memory_items",
             "search": {"title": "context", "text": "body",
                        "scope": "agent"}},
            {"id": "d2", "key": "agent_job_state", "collection": "br_agent_job_state"},
            {"id": "d3", "key": "agent_roi_injections",
             "collection": "br_agent_roi_injections"}]},
        "/types": {"types": [{"id": "br", "xKey": "agent_brain"}]},
        "/children": {"objectId": "brain1"},
        "/modify": {"recordIds": ["m1"]},
    }


def test_memory_verbs_and_paths():
    fx = wire(replies=_brain_wire())
    c = client(fx)
    assert c.get_brain("s1") == {"objectId": "brain1"}
    c.create_memory("s1", {"category": "fact", "context": "x"})
    c.evolve_memory("s1", "m1", {"accessCount": 2})
    c.delete_memory("s1", "m1")
    create = next(b for v, p, b in fx.calls if p.endswith("/modify"))
    assert create["objectId"] == "brain1"
    # the store key resolves to the brain's collection (ADR-027 §2)
    assert create["dataset"] == "br_agent_memory_items"
    assert create["records"][0]["id"] == ""      # auto-derived item id
    assert create["records"][0]["upsert"] is True
    set_fields = {op["path"]: op["value"]
                  for op in create["records"][0]["ops"]}
    # required + the scoring defaults (ADR-017: validation and
    # defaults are client-side now)
    assert set_fields["category"] == "fact"
    assert set_fields["confidence"] == 5 and set_fields["salience"] == 10
    assert set_fields["importance"] == 5 and set_fields["accessCount"] == 0
    assert "validFrom" in set_fields
    assert set_fields["validFrom"] == {"$date": 1787673600000}   # instant(now())
    evolve = [b for v, p, b in fx.calls if p.endswith("/modify")][1]
    assert evolve["records"][0]["id"] == "m1"
    assert "upsert" not in evolve["records"][0]
    assert fx.calls[-1][1] == "/v1/spaces/s1/delete-records"
    assert fx.calls[-1][2]["recordIds"] == ["m1"]
    assert fx.calls[-1][2]["dataset"] == "br_agent_memory_items"


def test_memory_validation_is_client_side():
    fx = wire(replies=_brain_wire())
    c = client(fx)
    err = load(wire())["AnyError"]  # class identity differs per load
    with pytest.raises(Exception, match="category required"):
        c.create_memory("s1", {"context": "x"})
    with pytest.raises(Exception, match="unknown memory fields"):
        c.create_memory("s1", {"category": "fact", "context": "x",
                               "embeddingRef": "nope"})
    with pytest.raises(Exception, match="not evolvable"):
        c.evolve_memory("s1", "m1", {"category": "flip"})
    with pytest.raises(Exception, match="confidence"):
        c.create_memory("s1", {"category": "fact", "context": "x",
                               "confidence": 11})
    assert err  # silence unused warning


# --- spaces & ui context ---------------------------------------------------------


def test_list_spaces_unwraps():
    fx = wire(replies={"/v1/spaces": {"spaces": [{"id": "sp1", "name": "bao",
                                                  "status": "active"}]}})
    assert client(fx).list_spaces() == [{"id": "sp1", "name": "bao",
                                         "status": "active"}]


def test_create_space_installs_the_derived_general_chat():
    # the POST is followed by the catalog's general-chat setup (ADR-027
    # §1), and the reply is the trimmed row + the chat id (no chat id
    # rides the space row)
    fx = wire(replies={
        "/v1/spaces": {"id": "sp9", "name": "AI Startups",
                       "push": {"encKey": "SECRET"}},
        "/catalog/general-chat/setup": _CHAT_SETUP})
    r = client(fx).create_space("AI Startups")
    assert r == {"id": "sp9", "name": "AI Startups", "generalChatId": "chat9"}
    # no spaceType: empty = server default on every vintage
    # ("anytype.space" is rejected since SDK v0.0.10)
    assert fx.calls == [
        ("POST", "/v1/spaces", {"name": "AI Startups"}),
        ("POST", "/v1/catalog/general-chat/setup", {"spaceId": "sp9"}),
    ]
    # description only rides the wire when given
    client(fx).create_space("x", description="d")
    assert fx.calls[-2][2] == {"name": "x", "description": "d"}


# --- list_programs (ADR-009 §2: repo browsing) --------------------------------

_PROG_REPLIES = {
    "/types": {"types": [{"id": "bafyPROG", "name": "Program", "xKey": "program"}]},
    "/types/any/properties": _ANY_PROPS,
    "/types/bafyPROG/properties": {"properties": [
        {"id": "bafyNAME", "name": "Name", "xKey": "name"},
        {"id": "bafyVER", "name": "Version", "xKey": "version"},
        {"id": "bafyTOOL", "name": "Any Tool", "xKey": "any_tool"},
        {"id": "bafySUM", "name": "Summary", "xKey": "summary"}]},
    "/objects/query": {"records": [
        {"id": "p1", "any": {"name": "webSearch", "types": ["bafyPROG"]},
         "bafyPROG": {"bafyNAME": "webSearch", "bafyVER": "v1",
                      "bafyTOOL": True,
                      "bafySUM": "Web search one-liner."}},
        {"id": "p2", "any": {"name": "helper", "types": ["bafyPROG"]},
         "bafyPROG": {"bafyNAME": "helper", "bafyVER": "v2",
                      "bafyTOOL": False}}]},
}


def test_list_programs_lists_a_space_sorted():
    # summary is the cached object property (ADR-010 §4) — a program
    # deployed without one just lists with an empty summary
    fx = wire(replies=dict(_PROG_REPLIES))
    rows = client(fx).list_programs("repo1")
    assert rows == [
        {"name": "helper", "version": "v2", "anyTool": False,
         "summary": ""},
        {"name": "webSearch", "version": "v1", "anyTool": True,
         "summary": "Web search one-liner."}]
    # the object query targeted the requested space with the program
    # filter, minus the type-definition row and binned objects
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"$and": [{"any.types": "bafyPROG"},
                                       {"any.types": {"$ne": "__type__"}},
                                       {"any.types": {"$nin": ["bin"]}}]}
    # one round-trip per listing — no per-program dataset reads
    assert not [p for v, p, b in fx.calls if p.endswith("/query")
                and not p.endswith("/objects/query")]


def test_list_programs_tools_only_filters():
    fx = wire(replies=dict(_PROG_REPLIES))
    rows = client(fx).list_programs("repo1", tools_only=True)
    assert [r["name"] for r in rows] == ["webSearch"]


# --- flat module surface (ADR-010 §8) ------------------------------------------

def test_list_devices_is_the_registry_read():
    # ADR-015 §5: the guest reads the registry as-is — self, the
    # server-computed winners, every registered device
    reg = {"self": "p1", "active": {"bao": "p2"},
           "devices": [{"peerId": "p1", "name": "laptop", "os": "linux", "apps": {"bao": {}}},
                       {"peerId": "p2", "name": "mac", "os": "darwin", "apps": {"bao": {}},
                        "activeClaims": {"bao": {"seq": 3, "at": 1}}}]}
    fx = wire(replies={"/v1/devices": reg})
    assert client(fx).list_devices() == reg
    assert fx.calls == [("GET", "/v1/devices", None)]


def test_dataset_field_helpers_hit_the_field_routes():
    # ADR-017 §1 additive evolution: one field in, one field out, the
    # declaration itself untouched (no PATCH, no re-declare)
    fx = wire(replies={"/types": {"types": [{"id": "mb", "xKey": "mailbox"}]},
                       "/fields": {"fieldDefId": "f9"}})
    c = client(fx)
    assert c.add_dataset_field("s1", "mailbox", "d1", {"key": "notes", "kind": "string",
                                                        "mutableBy": "any"}) == {"fieldDefId": "f9"}
    assert c.remove_dataset_field("s1", "mailbox", "d1", "f9") == {}
    assert [c for c in fx.calls if c[0] != "GET"] == [
        ("POST", "/v1/spaces/s1/types/mb/datasets/d1/fields",
         {"key": "notes", "kind": "string", "mutableBy": "any"}),
        ("DELETE", "/v1/spaces/s1/types/mb/datasets/d1/fields/f9", None),
    ]


def test_dataset_field_helpers_are_private_on_the_flat_surface():
    # the declaration tier stays program plumbing (ADR-010 §1 hides `_`
    # names from the inventory); the device read is public
    g = load(wire())
    assert "_add_dataset_field" in g and "_remove_dataset_field" in g
    assert "add_dataset_field" not in g and "remove_dataset_field" not in g
    assert "fieldDefId" in g["_add_dataset_field"].__doc__
    assert "MANUAL" in g["list_devices"].__doc__


def test_flat_functions_lift_method_docstrings():
    g = load(wire())
    assert "envelope" in g["search"].__doc__       # ONE authored copy, lifted
    assert "objectId" in g["create_object"].__doc__


def test_spaceconfig_accepts_id_row_and_ui_context_shapes():
    fx = wire(replies={"/types": {"types": []}},
              config={"any.base_url": "http://any"})
    g = load(fx)
    g["list_types"](SID)                    # bare id string
    g["list_types"]({"id": SID})            # a list_spaces() row
    g["list_types"]({"spaceId": SID})       # currentUserSpace shape
    assert [p for _, p, _ in fx.calls] == [f"/v1/spaces/{SID}/types"] * 3


def test_spaceconfig_misuse_errors_transparently():
    g = load(wire(config={"any.base_url": "http://any"},
                  replies={"/v1/spaces": {"spaces": [
                      {"id": "sp1", "name": "bao", "status": "active"}]}}))
    # query landed in the spaceConfig slot -> resolved as a NAME, fails
    # loud with the space list
    with pytest.raises(ValueError,
                       match='no space named "llm proxy" — spaces: "bao"'):
        g["search"]("llm proxy", "q")
    with pytest.raises(TypeError, match="spaceConfig"):
        g["list_types"](None)


def test_account_level_calls_take_no_spaceconfig():
    fx = wire(replies={"/v1/spaces": {"spaces": []}},
              config={"any.base_url": "http://any"})
    assert load(fx)["list_spaces"]() == []


# --- builtin-group path resolution (A19) ---------------------------------------

def test_any_derived_props_rewrite_to_top_level_keys():
    # the server advertises any.id/any.createdAt but stores them
    # TOP-LEVEL on records — the mapping layer honors the group form
    fx = wire(replies={**_CAT, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"any.id": "o1"},
                             sort=["-any.createdAt"])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"id": "o1"}
    assert body["sort"] == ["-createdAt"]


def test_any_synced_props_stay_group_addressed():
    fx = wire(replies={**_CAT, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"any.name": "README"})
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"any.name": "README"}


def test_unknown_builtin_prop_errors_with_the_catalog():
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError,
                       match='unknown property "bogus" on builtin group "any"'):
        client(fx).query_objects("s1", filter={"any.bogus": 1})
    # `nav` is no group: an unknown head errors with the catalog
    with pytest.raises(ValueError, match='type "nav" doesn.t exist'):
        client(fx).query_objects("s1", filter={"nav.parentId": "f1"})
    assert not any(p.endswith("/objects/query") for _, p, _ in fx.calls)


def test_program_group_paths_resolve_like_any_user_type():
    # toolcaller's compose filters {"program.any_tool": True} — `program`
    # is a harness-declared user type (ADR-010 §5), so the path resolves
    # to the space's typeId.propId like any other xKey path
    fx = wire(replies={**_PROG_REPLIES, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"program.any_tool": True})
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"bafyPROG.bafyTOOL": True}


# --- space-name resolution + row trim (ADR-010 §8 / A20) -----------------------

_SPACES = {"/v1/spaces": {"spaces": [
    {"id": "bafySPdev0000000000000.sfx", "name": "dev", "status": "active",
     "ownRole": "owner", "push": {"encKey": "SECRET"}},
    {"id": "bafySPlib0000000000000.sfx", "name": "library", "status": "active",
     "spaceIndexObjectId": "idx1", "settings": {"k": 1}},
    {"id": "bafySPold0000000000000.sfx", "name": "old", "status": "deleted"}]}}


def test_space_name_resolves_to_id_on_the_wire():
    fx = wire(replies={**_SPACES, "/types": {"types": []}},
              config={"any.base_url": "http://any"})
    g = load(fx)
    g["list_types"]("dev")
    g["list_types"]("LIBRARY")            # casefold-unique match
    paths = [p for _, p, _ in fx.calls]
    assert "/v1/spaces/bafySPdev0000000000000.sfx/types" in paths
    assert "/v1/spaces/bafySPlib0000000000000.sfx/types" in paths
    assert paths.count("/v1/spaces") == 1          # catalog memoized


def test_space_name_unknown_and_inactive_fail_loud():
    fx = wire(replies=_SPACES, config={"any.base_url": "http://any"})
    g = load(fx)
    with pytest.raises(ValueError,
                       match='no space named "devz" — spaces: "dev", "library"'):
        g["list_types"]("devz")
    with pytest.raises(ValueError, match='no space named "old"'):
        g["list_types"]("old")            # deleted spaces don't resolve


def test_space_name_ambiguity_fails_loud():
    fx = wire(replies={"/v1/spaces": {"spaces": [
        {"id": "a.aaaaaaaaaaaaaaaaaaaaaa", "name": "dev", "status": "active"},
        {"id": "b.bbbbbbbbbbbbbbbbbbbbbb", "name": "dev", "status": "active"}]}},
        config={"any.base_url": "http://any"})
    with pytest.raises(ValueError, match="ambiguous"):
        load(fx)["list_types"]("dev")


def test_id_shaped_spaceconfig_skips_the_catalog():
    fx = wire(replies={"/types": {"types": []}},
              config={"any.base_url": "http://any"})
    load(fx)["list_types"](SID)
    assert [p for _, p, _ in fx.calls] == [f"/v1/spaces/{SID}/types"]


def test_space_rows_are_trimmed_push_never_leaks():
    fx = wire(replies=_SPACES, config={"any.base_url": "http://any"})
    g = load(fx)
    rows = g["list_spaces"]()
    assert all("push" not in r and "settings" not in r
               and "spaceIndexObjectId" not in r for r in rows)
    assert rows[0] == {"id": "bafySPdev0000000000000.sfx", "name": "dev",
                       "status": "active", "ownRole": "owner"}
    raw = g["list_spaces"](raw=True)
    assert raw[0]["push"] == {"encKey": "SECRET"}   # escape hatch


def test_get_space_trims_sync_internals():
    fx = wire(replies={"/v1/spaces": {"spaces": []},
                       "/spaces/s1": {"id": "s1", "name": "dev",
                                      "push": {"encKey": "SECRET"},
                                      "spaceIndexObjectId": "idx",
                                      "settings": {"x": 1}}},
              config={"any.base_url": "http://any"})
    r = load(fx)["get_space"]("s1")
    assert r == {"id": "s1", "name": "dev"}


def test_search_types_kwarg_redirects_to_query_objects():
    # A18: the guessed types= kwarg gets the redirect, not a bare TypeError
    g = load(wire())
    with pytest.raises(TypeError, match="any.types"):
        g["search"](SID, "q", types=["task"])


def test_open_in_ui_publishes_device_scope_ui_events():
    # the agent→UI navigation directive: ui.* events on the DEVICE bus
    # (user decision 2026-08-19 — never the account's other machines)
    fx = wire(replies={"/v1/events": {"subscribers": 1}})
    c = client(fx)
    assert c.open_in_ui(SID) == {"subscribers": 1}
    c.open_in_ui(SID, "obj1")
    (v1, p1, b1), (v2, p2, b2) = fx.calls
    assert (v1, p1) == ("POST", "/v1/events")
    assert b1 == {"type": "ui.open_space", "scope": "device",
                  "data": {"spaceId": SID, "source": "bao"}}
    assert b2["type"] == "ui.open_object"
    assert b2["data"] == {"spaceId": SID, "objectId": "obj1",
                          "source": "bao"}


# --- ADR-019 §4: bare time literals are refused at the client boundary ---------

def test_dataset_query_refuses_bare_literal_on_stamp_and_datetime_field():
    fx = wire(replies={"/query": {"records": []}})
    c = client(fx)
    with pytest.raises(ValueError, match="createdAt.*instant"):
        c.query("s1", "log1", "agent_turns", filter={"createdAt": {"$gte": 100}})
    with pytest.raises(ValueError, match="validFrom"):
        c.query("s1", "b1", "agent_memory_items",
                filter={"$and": [{"validFrom": {"$lt": "2026-08-01"}}]})
    with pytest.raises(ValueError, match="periodStart"):
        c.query("s1", "log1", "agent_chunks", filter={"periodStart": 5})
    with pytest.raises(ValueError, match="createdAt"):
        c.query("s1", "chat1", "chat_messages", filter={"createdAt": {"$in": [1, 2]}})
    assert fx.calls == []            # nothing reached the wire


_LOG = {
    "/types": {"types": [{"id": "lg", "xKey": "agent_log"}]},
    "/types/lg/datasets": {"datasets": [
        {"id": "d1", "key": "agent_turns", "collection": "lg_agent_turns"}]},
    "/objects/query": {"records": [{"id": "log1", "any": {"types": ["lg"]}}]},
}


def test_dataset_query_passes_instants_and_unknown_keys():
    fx = wire(replies={**_LOG, "/query": {"records": []}})
    c = client(fx)
    lit = {"$date": 1787673600000}
    c.query("s1", "log1", "agent_turns",
            filter={"createdAt": {"$gte": lit, "$lte": {"$date": "2026-09-01T00:00:00Z"}},
                    "seq": {"$gt": 3}, "lastModifiedAt": {"$gt": 7}})
    assert fx.calls[-1][2]["filter"]["createdAt"]["$gte"] == lit
    c.query("s1", "log1", "agent_turns", filter={"createdAt": {"$exists": True}})
    c.query("s1", "log1", "agent_turns", filter={"createdAt": {"$in": [lit]}})


def test_create_dataset_draft_registers_its_datetime_keys():
    fx = wire(replies={**_CAT, "/types/bafyTASK/datasets": {"datasets": [
                           {"id": "d1", "key": "events", "collection": "bafyTASK_events"}]},
                       "/query": {"records": []}})
    c = client(fx)
    c.create_dataset("s1", "task", {"key": "events", "idRule": "user",
                                    "deleteBy": "anyone",
                                    "fields": [{"key": "at", "kind": "datetime"}]})
    with pytest.raises(ValueError, match='"at"'):
        c.query("s1", "o1", "events", filter={"at": {"$lt": 1}})


def test_objects_query_refuses_bare_literal_on_stamps_and_datetime_props():
    cat = {**_CAT, "/types/bafyTASK/properties": {"properties": [
        {"id": "bafyDUE", "name": "Due", "xKey": "due", "kind": "datetime",
         "xFormat": {"type": "date"}},
        {"id": "bafySTATUS", "name": "Status", "xKey": "status", "kind": "string"}]}}
    fx = wire(replies={**cat, "/objects/query": {"records": []}})
    c = client(fx)
    with pytest.raises(ValueError, match="createdAt"):
        c.query_objects("s1", filter={"any.createdAt": {"$gte": 1756058400}})
    with pytest.raises(ValueError, match="bafyDUE"):
        c.query_objects("s1", filter={"task.due": "2026-08-25"})
    c.query_objects("s1", filter={"task.status": "open",
                                  "task.due": {"$lt": {"$date": 1787673600000}},
                                  "modifiedAt": {"$gte": {"$date": "2026-08-01T00:00:00Z"}}})
    assert fx.calls[-1][2]["filter"]["bafyTASK.bafyDUE"] == {"$lt": {"$date": 1787673600000}}


def test_create_type_posts_property_descriptors_with_a_derived_kind():
    # a slug declared through create_type reaches the wire as xFormat
    # with the kind it implies (ADR-027 §4); hidden/weight/layout ride
    # the type create
    fx = wire(replies={**_CAT, "/types": {"types": [], "typeId": "tNew"},
                       "/types/tNew/properties": {"properties": [], "propId": "p1"}})
    client(fx).create_type("s1", {"name": "Event", "hidden": True, "properties": [
        {"name": "When", "xFormat": {"type": "datetime"}},
        {"name": "Title"}]})
    posted = [b for v, p, b in fx.calls if v == "POST" and p.endswith("/properties")]
    assert posted[0] == {"name": "When", "xKey": "when", "kind": "datetime",
                         "xFormat": {"type": "datetime", "pos": "a0"}}
    assert posted[1] == {"name": "Title", "xKey": "title", "kind": "string",
                         "xFormat": {"pos": "a0"}}
    tpost = next(b for v, p, b in fx.calls if v == "POST" and p.endswith("/types"))
    assert tpost == {"name": "Event", "xKey": "event", "hidden": True}


# --- files (ADR-020 §2) -------------------------------------------------------

REF = {"__blob": "sha256:" + "ab" * 32, "bytes": 11, "mime": "image/png"}


def test_file_content_returns_a_blob_and_parses_refs():
    seen = []

    def fx(name, payload):
        seen.append((name, payload))
        if name in ("config.get", "runtime.get"):
            return {"value": "http://any"}
        assert name == "http.get" and "response" not in payload
        return {"status": 200, "headers": {"content-type": "image/png"},
                "body": dict(REF)}

    g = load(fx)
    c = g["_Client"]("http://any")
    r = c.file_content("s-given", "any://f/s-in-uri/file9?variant=thumb")
    assert (r["fileId"], r["mime"], r["size"]) == ("file9", "image/png", 11)
    assert isinstance(r["blob"], g["Blob"]) and r["blob"].sha256 == REF["__blob"]
    assert seen[-1][1]["url"] == "http://any/v1/spaces/s-in-uri/files/file9/content?variant=thumb"
    # bare fileId → the given space
    c.file_content("s-given", "file9")
    assert seen[-1][1]["url"] == "http://any/v1/spaces/s-given/files/file9/content"
    # malformed URI is refused before any call
    n = len(seen)
    with pytest.raises(g["AnyError"]):
        c.file_content("s", "any://f/onlyone")
    assert len(seen) == n


def test_file_content_text_file_is_still_a_blob():
    # a text/* file comes back as text (ADR-026 §3) — wrapped into a handle
    fx = lambda n, p: {"status": 200, "headers": {"content-type": "text/markdown"},  # noqa: E731
                       "body": "# hi\n"}
    r = load(fx)["_Client"]("http://any").file_content("s", "f1")
    assert r["mime"] == "text/markdown" and r["size"] == 5
    assert r["blob"].text() == "# hi\n"


def test_attach_file_uploads_the_blob_as_the_body():
    seen = []

    def fx(name, payload):
        seen.append((name, payload))
        return {"status": 201, "headers": {},
                "body": json.dumps({"fileId": "f9", "objectId": "o1", "size": 11,
                                    "name": "rosé 1.png", "mime": "image/png"})}

    g = load(fx)
    c = g["_Client"]("http://any")
    b = g["Blob"].from_ref(REF)
    info = c.attach_file("s1", "o1", "rosé 1.png", b)
    assert info["uri"] == "any://f/s1/f9" and info["fileId"] == "f9"
    name, payload = seen[-1]
    assert name == "http.post"
    assert payload["url"] == "http://any/v1/spaces/s1/objects/o1/files?name=ros%C3%A9%201.png"
    assert payload["body"] is b and payload["headers"] == {"Content-Type": "image/png"}
    # bytes are wrapped into a Blob first; an explicit mime wins
    info = c.attach_file("s1", "o1", "a.csv", b"a,b\n", mime="text/csv")
    body = seen[-1][1]["body"]
    assert isinstance(body, g["Blob"]) and body.mime == "text/csv" and body.size == 4
    assert seen[-1][1]["headers"] == {"Content-Type": "text/csv"}
    # a server error is an AnyError
    fx_err = lambda n, p: {"status": 403, "headers": {},  # noqa: E731
                           "body": json.dumps({"error": {"code": "space.read_only",
                                                         "message": "guest"}})}
    g2 = load(fx_err)
    with pytest.raises(g2["AnyError"]) as e:
        g2["_Client"]("http://any").attach_file("s1", "o1", "x", b"1")
    assert e.value.code == "space.read_only"


def test_file_content_error_body_is_decoded():
    def fx(name, payload):
        body = json.dumps({"error": {"code": "file.not_available", "message": "not yet"}})
        return {"status": 409, "headers": {}, "body": body}

    g = load(fx)
    with pytest.raises(g["AnyError"]) as e:
        g["_Client"]("http://any").file_content("s", "f")
    assert e.value.status == 409 and e.value.code == "file.not_available"


def test_list_files_narrows_by_object():
    fx = wire(replies={"/files": {"files": [{"fileId": "f1", "mime": "text/plain"}]}})
    c = client(fx)
    assert c.list_files("s1", "o1") == [{"fileId": "f1", "mime": "text/plain"}]
    assert fx.calls == [("GET", "/v1/spaces/s1/files", None)]
    assert fx.urls == ["/v1/spaces/s1/files?objectId=o1"]


def test_file_not_available_carries_sync_hint():
    def fx(name, payload):
        body = json.dumps({"error": {"code": "file.not_available", "message": "no bytes"}})
        return {"status": 409, "headers": {}, "body": body}

    g = load(fx)
    with pytest.raises(g["AnyError"]) as e:
        g["_Client"]("http://any").file_content("s", "f")
    assert "not synced" in str(e.value)


def test_list_search_scopes_unions_fixed_and_declared():
    # discovery rows name their OWNERS (the declaring types); every
    # records owner is walked once for its declared scopes
    fx = wire(replies={
        "/v1/spaces/s1/datasets": {"datasets": [
            {"name": "chat_messages", "module": "chat", "shared": True,
             "owners": ["bafyreichat0000000000000000"]},
            {"name": "bafyreimailbox00000000000000_email_messages", "module": "records",
             "owners": ["bafyreimailbox00000000000000"]},
            {"name": "bafyreiagentlog0000000000000_agent_turns", "module": "records",
             "owners": ["bafyreiagentlog0000000000000"]}]},
        "bafyreimailbox00000000000000/datasets": {"datasets": [
            {"key": "email_messages",
             "search": {"title": "subject", "text": ["from", "body"], "scope": "email"}}]},
        "bafyreiagentlog0000000000000/datasets": {"datasets": [
            {"key": "agent_turns", "search": {"text": "text", "scope": "history"}},
            {"key": "agent_chunks", "search": {"text": "summary", "scope": "history"}}]}})
    c = client(fx)
    assert c.list_search_scopes("s1") == ["basic", "chat", "email", "history", "props"]
    # module collections are not walked — one call per records owner
    assert [p for _, p, _ in fx.calls] == [
        "/v1/spaces/s1/datasets",
        "/v1/spaces/s1/types/bafyreiagentlog0000000000000/datasets",
        "/v1/spaces/s1/types/bafyreimailbox00000000000000/datasets"]


# --- apps (ADR-027 §5) ---------------------------------------------------------

_CATALOG = {"usecases": [
    {"id": "wiki", "name": "Wiki", "description": "A tree of pages",
     "bundles": [{"id": "system:wiki/v1", "name": "Wiki"}]},
    {"id": "general-chat", "name": "General chat", "description": "The space's chat",
     "bundles": [{"id": "system:general-chat/v1", "name": "General"}]},
    {"id": "crm", "name": "CRM", "description": "Deals", "requires": ["contacts"],
     "bundles": [{"id": "system:deal/v1"}, {"id": "system:crm/v1"}]}]}


def test_list_apps_joins_sidebar_registry_and_catalog():
    fx = wire(replies={
        "/v1/catalog": _CATALOG, "/types/any/properties": _ANY_PROPS,
        "/types": {"types": [{"id": "miniapp", "xKey": "miniapp", "hidden": True,
                              "builtIn": True}]},
        "/objects/query": {"records": [
            {"id": "wk", "any": {"name": "Wiki", "types": ["__type__", "miniapp"]},
             "miniapp": {"bundle": "system:wiki/v1", "pos": "a0"}},
            {"id": "ch", "any": {"name": "General", "description": "Team talk",
                                 "types": ["__type__", "ch", "miniapp"]},
             "miniapp": {"bundle": "system:general-chat/v1", "hidden": True}},
            {"id": "nb", "any": {"name": "Notebook", "types": ["page", "miniapp"]},
             "miniapp": {"pos": "a2"}}]},
        "/bundles": {"bundles": [{"id": "system:wiki/v1"},
                                 {"id": "system:general-chat/v1"}]}})
    c = client(fx)
    assert c.list_apps("s1") == [
        {"name": "Wiki", "rootId": "wk", "description": "A tree of pages",
         "hidden": False, "pinned": False, "bundleId": "system:wiki/v1",
         "usecase": "wiki"},
        {"name": "General", "rootId": "ch", "description": "Team talk",
         "hidden": True, "pinned": False, "bundleId": "system:general-chat/v1",
         "usecase": "general-chat"},
        {"name": "Notebook", "rootId": "nb", "description": "", "hidden": False,
         "pinned": True}]
    q = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert q == {"filter": {"$and": [{"any.types": "miniapp"},
                                     {"any.types": {"$nin": ["bin"]}}]},
                 "sort": ["miniapp.pos"]}
    assert c.list_available_apps("s1") == [
        {"usecase": "wiki", "name": "Wiki", "description": "A tree of pages",
         "requires": [], "installed": True},
        {"usecase": "general-chat", "name": "General chat",
         "description": "The space's chat", "requires": [], "installed": True},
        {"usecase": "crm", "name": "CRM", "description": "Deals",
         "requires": ["contacts"], "installed": False}]
    assert [p for _, p, _ in fx.calls].count("/v1/catalog") == 1   # memoized


def test_setup_app_installs_and_reports_ids():
    fx = wire(replies={"/catalog/crm/setup": {"usecase": "crm", "bundles": [
        {"usecase": "contacts", "id": "system:contacts/v1", "installed": False,
         "bundle": {"rootId": "r1"}},
        {"usecase": "crm", "id": "system:deal/v1", "installed": True,
         "bundle": {"rootId": "r2"}, "typeId": "r2",
         "properties": {"stage": "pS", "amount": "pA"}}]}})
    assert client(fx).setup_app("s1", "crm") == [
        {"usecase": "contacts", "bundleId": "system:contacts/v1", "rootId": "r1",
         "installed": False},
        {"usecase": "crm", "bundleId": "system:deal/v1", "rootId": "r2",
         "installed": True, "typeId": "r2", "properties": {"stage": "pS", "amount": "pA"}}]
    assert fx.calls == [("POST", "/v1/catalog/crm/setup", {"spaceId": "s1"})]
