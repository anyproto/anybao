"""programs/any@v1 — the guest space-data client, tested host-side by
exec-ing the module source with a fake `effect` global answering
http.* (json wire replies) and config.get."""

import inspect
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
def client(fx, base="http://any", bao=None):
    return load(fx)["_Client"](base, bao)


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
    "/objects/query": {"records": [{"id": "obj1", "any": {"type": "bafyTRG"}}]},
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
    # a key the object's type does not declare errors with the list
    with pytest.raises(ValueError, match='declares no dataset "ghost".*agent_triggers'):
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
    # the catalog spans BOTH definition surfaces (one handle namespace,
    # ADR-029 §2): every catalog read is a types + a collections GET
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1/types"),          # the default type `page` resolves
        ("GET", "/v1/spaces/s1/collections"),
        ("POST", "/v1/spaces/s1/objects"),
        ("GET", "/v1/spaces/s1/types"),          # idempotency probe, both surfaces
        ("GET", "/v1/spaces/s1/collections"),
        ("GET", "/v1/catalog"),                  # catalog handles are reserved (once per run)
        ("GET", "/v1/spaces/s1/types/any/properties"),  # record-root keys are reserved (BOB-68)
        ("POST", "/v1/spaces/s1/types"),
        ("GET", "/v1/spaces/s1/types/t2/datasets"),     # the default type: a body (ADR-029 §3)
        ("POST", "/v1/spaces/s1/types/t2/parts"),
        ("GET", "/v1/spaces/s1/bundles"),        # collections app probe (§5)
        ("POST", "/v1/catalog/collections/setup"),
        ("GET", "/v1/spaces/s1/types"),          # add_property owner resolution
        ("GET", "/v1/spaces/s1/collections"),
        ("GET", "/v1/spaces/s1/types/t1/properties"),   # xFormat.pos append (ADR-027 §4)
        ("POST", "/v1/spaces/s1/types/t1/properties")]
    part = next(b for v, p, b in fx.calls if p.endswith("/parts"))
    assert part == {"key": "body", "datasets": [{"module": "editor", "shared": True}]}


# --- create_type: the anyHelper composite --------------------------------------

def test_create_type_composite_fans_out_properties():
    fx = wire(replies={
        "/types": {"types": [], "typeId": "t9"},
        "/bundles": {"bundles": [{"id": "system:collections/v1"}], "synced": True},
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
    # the space already has the collections app: no setup
    assert not any(p.endswith("/collections/setup") for v, p, _ in fx.calls)


def test_create_type_refuses_catalog_types():
    # a catalog app's type (wiki, person, …) is reserved: never created
    # or reshaped by create_type — `setup_app` installs the app
    fx = wire(replies={
        "/types": {"types": [{"id": "tw", "xKey": "wiki", "name": "Wiki"}], "typeId": "t9"},
        "/catalog": {"usecases": [{"id": "wiki", "bundles": [
            {"id": "system:wiki/v1", "type": {"xKey": "wiki"}}]}]},
        "/bundles": {"bundles": [{"id": "system:collections/v1"}]}})
    c = client(fx)
    with pytest.raises(ValueError, match="catalog app"):
        c.create_type("s1", {"name": "Wiki", "properties": [{"name": "Extra"}]})
    assert not any(v == "POST" for v, _, _ in fx.calls)      # nothing minted or reshaped
    # a server without a catalog reserves nothing
    fx = wire(replies={"/types": {"types": [], "typeId": "t9"},
                       "/bundles": {"bundles": [{"id": "system:collections/v1"}]}},
              status=404)
    fx404 = fx
    ok = wire(replies={"/types": {"types": [], "typeId": "t9"},
                       "/bundles": {"bundles": [{"id": "system:collections/v1"}]}})

    def fx_mixed(name, payload):   # the catalog 404s, everything else answers
        if payload.get("url", "").endswith("/v1/catalog"):
            return fx404(name, payload)
        return ok(name, payload)
    fx_mixed.calls = ok.calls
    assert client(fx_mixed).create_type("s1", {"name": "Plant"})["created"] is True


def test_collection_key_is_shape_based_and_offline():
    c = client(wire())
    assert c._collection_key("bafyreibrain0000000000000_agent_memory_items") == "agent_memory_items"
    # another multibase prefix — the id is recognised by shape, not by "bafy"
    assert c._collection_key("zb2rhXYZabc0123456789abcdef_agent_turns") == "agent_turns"
    assert c._collection_key("editor_blocks") == "editor_blocks"          # canonical
    assert c._collection_key("chat_messages") == "chat_messages"
    assert c._collection_key("agent_turns") == "agent_turns"              # a bare key


def test_aggregate_over_records_needs_object_and_dataset():
    c = client(wire(replies={"/aggregate": {"records": []}}))
    with pytest.raises(ValueError, match="BOTH object_id and dataset"):
        c.aggregate("s1", [{"$count": "n"}], dataset="email_messages")
    with pytest.raises(ValueError, match="BOTH object_id and dataset"):
        c.aggregate("s1", [{"$count": "n"}], object_id="o1")


def test_create_type_sets_up_the_collections_app_once():
    # a listed user type is invisible in the client until the space
    # has the collections app (ADR-027 §5): minted once per space
    fx = wire(replies={"/types": {"types": [], "typeId": "t9"},
                       "/bundles": {"bundles": [], "synced": True},
                       "/types/t9/properties": {"properties": []}})
    c = client(fx)
    c.create_type("s1", {"name": "Plant"})
    c.create_type("s1", {"name": "Pot"})
    c.create_type("s1", {"name": "Agent Thing", "hidden": True})   # hidden: never
    setups = [(v, p, b) for v, p, b in fx.calls if p.endswith("/collections/setup")]
    assert setups == [("POST", "/v1/catalog/collections/setup", {"spaceId": "s1"})]


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
    # no type POST, one prop; the body part is healed onto the existing
    # type that lacks it (ADR-029 §3)
    assert posts == ["/v1/spaces/s1/types/t9/properties", "/v1/spaces/s1/types/t9/parts"]
    # a type that already declares a body is left alone
    fx2 = wire(replies={
        "/types": {"types": [{"id": "t9", "name": "Task", "xKey": "task"}]},
        "/types/t9/datasets": _TASK_BODY,
        "/types/t9/properties": {"properties": [{"id": "p1", "xKey": "status"}]}})
    client(fx2).create_type("s1", {"name": "Task", "properties": [{"name": "status"}]})
    assert not [p for v, p, _ in fx2.calls if v == "POST"]
    # `"body": False` keeps a store bodiless (no datasets read at all)
    fx3 = wire(replies={"/types": {"types": [], "typeId": "t1"},
                        "/types/any/properties": {}})
    client(fx3).create_type("s1", {"name": "Agent Log", "xKey": "agent_log",
                                   "hidden": True, "body": False})
    assert not any(p.endswith("/datasets") or p.endswith("/parts") for _, p, _ in fx3.calls)
    # the record-root guard gates minting only: ensuring an existing type
    # reads no `any` catalog (the fixture would 404 it)
    assert "/v1/spaces/s1/types/any/properties" not in [p for _, p, _ in fx.calls]


def test_create_type_existing_root_key_handle_is_reused():
    # a type minted before the BOB-68 rule keeps its handle: ensure reuses
    # it (and can still reshape it) instead of refusing
    fx = wire(replies={
        "/types": {"types": [{"id": "tA", "name": "Author", "xKey": "author"}]},
        "/types/tA/properties": {"properties": [], "propId": "p1"}})
    r = client(fx).create_type("s1", {"name": "Author",
                                      "properties": [{"name": "bio"}]})
    assert r == {"typeId": "tA", "xKey": "author", "created": False,
                 "addedProps": {"bio": "p1"}}


def test_create_type_builtin_handle_errors():
    fx = wire(replies={
        "/types": {"types": [{"id": "type", "name": "Type", "xKey": "type"}]}})
    with pytest.raises(ValueError, match="builtin"):
        client(fx).create_type("s1", {"name": "Type"})
    with pytest.raises(ValueError, match="builtin"):
        client(fx).create_type("s1", {"name": "My Meta", "xKey": "type"})
    assert [v for v, _, _ in fx.calls if v == "POST"] == []


def test_create_type_row_root_key_errors():
    # BOB-68: derived `any` props are row-root keys; a type group under
    # the same xKey would shadow them on normalized reads.
    fx = wire(replies={"/types": {"types": []}, "/types/any/properties": {
        "properties": [{"id": "author", "scope": "derived"},
                       {"id": "pinned", "scope": "derived"}]}})
    with pytest.raises(ValueError, match='record-root.*"author_type"'):
        client(fx).create_type("s1", {"name": "Author"})
    with pytest.raises(ValueError, match="record-root"):
        client(fx).create_type("s1", {"name": "Created", "xKey": "createdAt"})
    with pytest.raises(ValueError, match="record-root"):   # live catalog slice
        client(fx).create_type("s1", {"name": "Pinned"})
    assert [v for v, _, _ in fx.calls if v == "POST"] == []
    fx = wire(replies={"/types/any/properties": {},
                       "/types": {"types": [], "typeId": "tA"}})
    r = client(fx).create_type("s1", {"name": "Author", "xKey": "author_type"})
    assert r["xKey"] == "author_type" and r["created"]
    assert ("POST", "/v1/spaces/s1/types",
            {"name": "Author", "xKey": "author_type"}) in fx.calls


def test_create_object_rejects_synthetic_types():
    fx = wire()
    with pytest.raises(ValueError, match="synthetic"):
        client(fx).create_object("s1", {"type": "type"})
    with pytest.raises(ValueError, match="synthetic"):
        client(fx).create_object("s1", {"type": "page", "collections": ["spaceIndex"]})
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
# `task` has a body: every type bao mints declares the shared editor
# part (ADR-029 §3)
_TASK_BODY = {"datasets": [
    {"id": "dBODY", "key": "editor_blocks", "collection": "editor_blocks",
     "module": "editor", "shared": True, "partId": "pBODY"}]}
_CAT = {
    "/types": {"types": [
        {"id": "bafyTASK", "name": "Task", "xKey": "task"},
        {"id": "page", "name": "page", "xKey": "page", "hidden": True, "builtIn": True}]},
    "/types/bafyTASK/datasets": _TASK_BODY,
    "/types/any/properties": _ANY_PROPS,
    "/types/bafyTASK/properties": {"properties": [
        {"id": "bafySTATUS", "name": "Status", "xKey": "status"},
        {"id": "bafyPRIO", "name": "Priority", "xKey": "priority"}]}}


def test_query_objects_normalizes_user_groups_keeps_builtins():
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "any": {"name": "Ship", "type": "bafyTASK"},
         "bafyTASK": {"bafySTATUS": "open", "bafyPRIO": 3}}]}})
    [rec] = client(fx).query_objects("s1", filter={"any.type": "task"})
    # user group + its props rekeyed to xKeys; the any.type VALUE too;
    # builtins (page, id) verbatim
    assert rec == {"id": "o1", "any": {"name": "Ship", "type": "task"},
                   "task": {"status": "open", "priority": 3}}


def test_query_objects_resolves_filter_and_sort_xkey_paths():
    fx = wire(replies={**_CAT, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"any.type": "task",
                                           "task.status": "open"},
                             sort=["-task.priority"])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    # any.type VALUE + dotted xKey paths resolved to server ids; builtin passthrough
    assert body["filter"] == {"any.type": "bafyTASK",
                              "bafyTASK.bafySTATUS": "open"}
    assert body["sort"] == ["-bafyTASK.bafyPRIO"]


def test_create_object_resolves_types_and_property_groups():
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    client(fx).create_object("s1", {
        "type": "task",
        "initialProperties": {"any": {"name": "Ship it"},
                              "task": {"status": "open", "priority": 3}}})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    # one type, no collections key when filed under none
    assert body == {"type": "bafyTASK", "initialProperties": {
        "any": {"name": "Ship it"},                    # reserved: literal
        "bafyTASK": {"bafySTATUS": "open", "bafyPRIO": 3}}}


def test_create_object_unknown_property_raises_never_drops():
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    with pytest.raises(ValueError, match='unknown property "nope" on "task"'):
        client(fx).create_object("s1", {
            "type": "task", "initialProperties": {"task": {"nope": 1}}})
    # nothing was written — the object POST never fired
    assert not any(p == "/v1/spaces/s1/objects" for v, p, _ in fx.calls)


def test_create_object_unknown_type_lists_available():
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match='type "ghost" doesn.t exist'):
        client(fx).create_object("s1", {"type": "ghost"})


def test_space_argument_must_be_a_string():
    # a list_spaces() row (or the list itself) passed as `space` must
    # fail at the boundary, not frames deep as an unhashable dict key
    fx = wire(replies=_CAT)
    with pytest.raises(TypeError, match="space must be a space id string"):
        client(fx).query_objects(["s1"], filter={"any.type": "task"})


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
    client(fx).create_object("s1", {"type": "task", "name": "Dune",
                                    "description": "a note"})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body["initialProperties"]["any"] == {"name": "Dune",
                                                "description": "a note"}
    assert "name" not in body and "description" not in body


def test_create_object_markdown_writes_the_body_after_create():
    # one call creates a typed page: the object's ONE type declares the
    # body (ADR-029 §3) and markdown (alias `body`) rides as a
    # put_markdown after the create
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"},
                       "/editor/editor_blocks/markdown": {}})
    client(fx).create_object("s1", {"type": "task", "name": "Dune",
                                    "markdown": "# Dune\n\nsand"})
    paths = [(v, p) for v, p, _ in fx.calls]
    i_create = paths.index(("POST", "/v1/spaces/s1/objects"))
    i_md = paths.index(("PUT", "/v1/spaces/s1/objects/o9/editor/editor_blocks/markdown"))
    assert i_create < i_md
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert "markdown" not in body
    assert body["type"] == "bafyTASK" and "collections" not in body
    md = next(b for v, p, b in fx.calls if p.endswith("/editor/editor_blocks/markdown"))
    assert md["content"] == "# Dune\n\nsand"
    # no membership round-trip: the type carries the body
    assert not any("/properties/" in p for _, p, _ in fx.calls)


def test_create_object_defaults_to_page_and_refuses_the_old_shape():
    # no `type` = a plain document (ADR-029 §3); `types` is a hard
    # error naming the two slots — the wire would 400 anyway, but only
    # after the handles resolved
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"}})
    c = client(fx)
    c.create_object("s1", {"name": "Note", "markdown": "hi"})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body == {"type": "page", "initialProperties": {"any": {"name": "Note"}}}
    with pytest.raises(ValueError, match='no "types".*"collections"'):
        c.create_object("s1", {"types": ["task"]})


def test_create_object_files_under_collections_and_guards_the_slots():
    # a contact is a person FILED UNDER contact (ADR-029 §6): the type
    # slot takes a type, the collections slot collections — the wrong
    # kind in either is refused with the verb that fits
    cat = {**_CAT, "/collections": {"collections": [
        {"id": "bafyCONTACT", "name": "Contact", "xKey": "contact"}]},
        "/collections/bafyCONTACT/properties": {"properties": [
            {"id": "pSTAGE", "name": "Stage", "xKey": "stage"}]},
        "/objects": {"objectId": "o9"}}
    fx = wire(replies=cat)
    c = client(fx)
    c.create_object("s1", {"type": "task", "collections": ["contact"],
                           "initialProperties": {"contact": {"stage": "warm"}}})
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    # the collection's group is keyed by ITS id — the owner of the values
    assert body == {"type": "bafyTASK", "collections": ["bafyCONTACT"],
                    "initialProperties": {"bafyCONTACT": {"pSTAGE": "warm"}}}
    with pytest.raises(ValueError, match="is a collection, not a type.*add_to_collection"):
        c.create_object("s1", {"type": "contact"})
    with pytest.raises(ValueError, match="is a type, not a collection.*set_type"):
        c.create_object("s1", {"collections": ["task"]})


def test_create_object_markdown_needs_a_body_on_the_type():
    # a type without an editor part cannot hold markdown: an error
    # naming the fix, never a silent retype to page (ADR-029 §3)
    cat = {**_CAT, "/types/bafyTASK/datasets": {"datasets": []}}
    fx = wire(replies=cat)
    with pytest.raises(ValueError, match='type "task" declares no body.*create_type'):
        client(fx).create_object("s1", {"type": "task", "markdown": "x"})
    assert not any(p == "/v1/spaces/s1/objects" for v, p, _ in fx.calls)


def test_create_object_parent_places_it_in_the_wiki_tree():
    # parent= → the catalog's wiki usecase (set up once per space per
    # run) — a COLLECTION the object is filed under, parentId + a
    # position after the last sibling under it (ADR-029 §5); the type
    # is whatever the caller said
    wiki = {"usecase": "wiki", "bundles": [{
        "id": "system:wiki/v1", "installed": False, "typeId": None,
        "collectionId": "bafyWIKI",
        "bundle": {"id": "system:wiki/v1", "rootId": "bafyWIKI"},
        "properties": {"parentId": "pPAR", "pos": "pPOS", "folder": "pFOL"}}]}
    replies = {**_CAT, "/objects": {"objectId": "o9"},
               "/collections": {"collections": [
                   {"id": "bafyWIKI", "name": "Wiki", "xKey": "wiki"}]},
               "/catalog/wiki/setup": wiki,
               "/objects/query": {"records": [{"id": "sib", "bafyWIKI": {"pPOS": "a3"}}]}}
    fx = wire(replies=replies)
    c = client(fx)
    c.create_object("s1", {"type": "task", "name": "Dune"}, parent="", folder=True)
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert body["type"] == "bafyTASK" and body["collections"] == ["bafyWIKI"]
    assert body["initialProperties"]["bafyWIKI"] == {"pPAR": "", "pPOS": "a4", "pFOL": True}
    sib = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert sib == {"filter": {"bafyWIKI.pPAR": ""}, "sort": ["-bafyWIKI.pPOS"], "limit": 1}
    # the setup ran once; a second placed object reuses it
    c.create_object("s1", {"name": "Heat"}, parent="o9")
    assert [p for _, p, _ in fx.calls].count("/v1/catalog/wiki/setup") == 1
    # move_object files the object under the wiki when it is not yet
    # and re-places it under the collection's group
    fx.calls.clear()
    c.move_object("s1", "o1", "o9")
    verbs = [(v, p.split("/s1/")[1]) for v, p, _ in fx.calls if v != "GET"]
    assert ("POST", "properties/o1/collections/bafyWIKI") in verbs
    assert ("POST", "properties/o1/set/bafyWIKI") in verbs
    # "take it out of the wiki" is an unfile, the type untouched
    fx.calls.clear()
    assert c.remove_from_collection("s1", "o1", "wiki") == {}
    assert fx.calls[-1][:2] == ("DELETE", "/v1/spaces/s1/properties/o1/collections/bafyWIKI")


def test_create_object_unknown_top_level_key_raises_never_posts():
    # the wire accepts only type/collections/initialProperties and rejects the
    # rest — the client refuses first instead of losing intent (and
    # `nav` is no key at all: the tree is `parent=`)
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match="unknown top-level key"):
        client(fx).create_object("s1", {"type": "task",
                                        "any": {"name": "x"}})
    with pytest.raises(ValueError, match="parent= places"):
        client(fx).create_object("s1", {"nav": {"parentId": ""}})
    assert not any(p == "/v1/spaces/s1/objects" for v, p, _ in fx.calls)


def test_query_objects_unknown_opt_raises_never_queries():
    # a typo'd opt (filters=) is an unvisited key server-side: the
    # query would silently match EVERY object in the space
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match="unknown option"):
        client(fx).query_objects("s1", filters={"any.type": "task"})
    assert not any(p.endswith("/objects/query") for v, p, _ in fx.calls)


def test_query_filters_error_on_unknown_keys_never_silent_empty():
    # the store answers a typo'd key with a silent empty set — the
    # resolution layer must refuse to forward what it can't resolve
    # (ADR-006 §6, reads like writes)
    fx = wire(replies=_CAT)
    c = client(fx)
    with pytest.raises(ValueError, match='type "unicorn" doesn.t exist'):
        c.query_objects("s1", filter={"any.type": "unicorn"})
    with pytest.raises(ValueError, match='type "unicorn" doesn.t exist'):
        c.query_objects("s1", filter={"any.type": {"$in": ["task", "unicorn"]}})
    with pytest.raises(ValueError, match='unknown property "nope" on "task"'):
        c.query_objects("s1", filter={"task.nope": 1})
    with pytest.raises(ValueError, match='"bookz" is neither a type nor a collection'):
        c.query_objects("s1", filter={"bookz.rating": {"$gte": 5}})
    # the old plural is a deliberate error naming both slots — the
    # server would answer it with a silent [] (ADR-029 §2)
    with pytest.raises(ValueError, match='no "any.types".*any.collections'):
        c.query_objects("s1", filter={"any.types": "task"})
    with pytest.raises(ValueError, match='no "any.types"'):
        c.query_objects("s1", sort=["-any.types"])
    with pytest.raises(ValueError, match='unknown property "nope"'):
        c.query_objects("s1", sort=["-task.nope"])
    # nothing reached the wire beyond catalog reads
    assert not any(p.endswith("/objects/query") for _, p, _ in fx.calls)
    # data-dependent empties are untouched: resolvable key, no matches
    assert c.query_objects("s1", filter={"task.status": "open"}) == []


def test_update_object_writes_name_markdown_and_prop_groups():
    # o1's type declares the body: the write goes straight through, no
    # membership call (no write sets a type — ADR-029 §3)
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "any": {"type": "bafyTASK"}}]}})
    r = client(fx).update_object("s1", "o1", {
        "name": "Renamed", "description": "now with mangoes",
        "markdown": "# body", "task": {"status": "done"}})
    assert r == {"objectId": "o1"}
    posts = [(p, b) for v, p, b in fx.calls if v in ("POST", "PUT")]
    assert ("/v1/spaces/s1/objects/o1/editor/editor_blocks/markdown",
            {"content": "# body"}) in posts
    assert not any("/properties/o1/type/" in p or "/collections/" in p for p, _ in posts)
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
    c.create_object("s1", {"type": "task"})   # miss then hit
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


def test_create_dataset_module_part_gives_the_type_a_body():
    # a shared editor part (ADR-027 §3): declared under `body` unless the
    # type already holds editor_blocks; no key on a module dataset
    fx = wire(replies={"/types": {"types": [{"id": "pr", "xKey": "program"}]},
                       "/types/pr/datasets": {"datasets": [
                           {"id": "d1", "key": "editor_blocks", "collection": "editor_blocks",
                            "module": "editor", "shared": True}]}})
    r = client(fx).create_dataset("s1", "program", {"module": "editor", "shared": True})
    assert r == {"datasetDefId": "d1", "collection": "editor_blocks", "created": False}
    assert not [c for c in fx.calls if c[0] == "POST"]
    fx = wire(replies={"/types": {"types": [{"id": "pr", "xKey": "program"}]},
                       "/parts": {"partId": "p1"}})
    client(fx).create_dataset("s1", "program", {"module": "editor", "shared": True})
    part = next(b for v, p, b in fx.calls if p.endswith("/parts"))
    assert part == {"key": "body", "datasets": [{"module": "editor", "shared": True}]}
    with pytest.raises(ValueError, match="shared"):
        client(fx).create_dataset("s1", "program", {"module": "editor"})


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
        ("GET", "/v1/spaces/s1/types"),   # list_properties owner resolution
        ("GET", "/v1/spaces/s1/collections"),
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
    with pytest.raises(ValueError, match='"ghost" is neither a type nor a collection'):
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
        {"id": "bafyTASK", "count": 2},
        {"id": "page", "count": 1}]}})
    r = client(fx).aggregate("s1", [{"$group": {"_id": "$any.type",
                                                "count": {"$sum": 1}}}])
    assert r["records"] == [{"id": "task", "count": 2},
                            {"id": "page", "count": 1}]


def test_aggregate_resolves_xkey_field_refs_in_pipeline():
    fx = wire(replies={**_CAT, "/objects/aggregate": {"records": []}})
    client(fx).aggregate("s1", [
        {"$match": {"any.type": "task", "task.priority": {"$gte": 2}}},
        {"$group": {"_id": "$task.status",
                    "avg": {"$avg": "$task.priority"},
                    "n": {"$sum": 1}}},
        {"$sort": {"task.priority": -1, "n": 1}}])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/aggregate"))
    assert body["pipeline"] == [
        {"$match": {"any.type": "bafyTASK",
                    "bafyTASK.bafyPRIO": {"$gte": 2}}},
        {"$group": {"_id": "$bafyTASK.bafySTATUS",
                    "avg": {"$avg": "$bafyTASK.bafyPRIO"},
                    "n": {"$sum": 1}}},   # int + non-ref strings untouched
        {"$sort": {"bafyTASK.bafyPRIO": -1, "n": 1}}]


def test_aggregate_pipeline_unknown_ref_errors_literals_pass():
    fx = wire(replies=_CAT)
    c = client(fx)
    with pytest.raises(ValueError, match='unknown property "nope" on "task"'):
        c.aggregate("s1", [{"$group": {"_id": "$task.nope"}}])
    with pytest.raises(ValueError, match='"ghost" is neither a type nor a collection'):
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
    # o1 is a `page`: the routes name the shared editor collection
    # (ADR-029 §3); one object read decides the body gate
    fx = wire(replies={"/editor/editor_blocks/markdown": {"content": "# hi"},
                       "/objects/query": {"records": [
                           {"id": "o1", "any": {"type": "page"}}]}})
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
    # one object read decided both writes pass the body gate
    assert [p for _, p, _ in fx.calls].count("/v1/spaces/s1/objects/query") == 1


# --- outgoing link shape (ADR-010 §8 amendment 2026-09-09) --------------------

SPACES = {"/v1/spaces": {"spaces": [
    {"id": SID, "name": "ta", "status": "active"}]}}


def test_markdown_writers_warn_on_a_space_name_in_a_link(capsys):
    fx = wire(replies={**SPACES,
                       "/objects/query": {"records": [
                           {"id": "o1", "any": {"type": "page"}}]},
                       "/markdown": {"inserted": 1}})
    c = client(fx)
    r = c.put_markdown("s1", "o1", "[x.docx](any://f/ta/2yp2VRDcFqu)")
    assert r["inserted"] == 1
    assert r["warnings"] == [
        'any://f/ta/2yp2VRDcFqu: "ta" is a space NAME — the space segment '
        f"of a link is its id: any://f/{SID}/2yp2VRDcFqu"]
    # the body shipped verbatim — no rewrite
    put = next(b for v, p, b in fx.calls if v == "PUT")
    assert put == {"content": "[x.docx](any://f/ta/2yp2VRDcFqu)"}
    # and the warning also reaches the cell digest as a printed line
    assert capsys.readouterr().out.startswith("warning: any://f/ta/")


def test_markdown_writers_warn_on_a_missing_space_segment():
    fx = wire(replies={"/objects/query": {"records": [
                           {"id": "o1", "any": {"type": "page"}}]},
                       "/append": {"inserted": 1}})
    c = client(fx)
    r = c.append_markdown("s1", "o1", "see [Page](any://o/bafyobj1) and "
                          "[again](any://o/bafyobj1)")
    assert r["warnings"] == [
        "any://o/bafyobj1: no space segment — a typed link is any://o/<spaceId>/<id>"]
    r = c.edit_markdown("s1", "o1", [{"oldText": "a", "newText": "[s](any://s)"}])
    assert r["warnings"] == [
        "any://s: no space segment — a typed link is any://s/<spaceId>/<id>"]


def test_well_shaped_and_legacy_links_pass_silently():
    fx = wire(replies={**SPACES,
                       "/objects/query": {"records": [
                           {"id": "o1", "any": {"type": "page"}}]},
                       "/markdown": {"inserted": 1}})
    c = client(fx)
    body = (f"[a](any://o/{SID}/bafyobj1) ![i](any://f/{SID}/fid?w=1) "
            f"[m](any://m/{SID}/ident) [sp](any://s/{SID}) "
            "legacy any://bafyobj1 and any://bafyspace0000000000000000.x/bafyobj1 "
            "and [rec](any://o/" + SID + "/o/editor_blocks/b1)")
    assert c.put_markdown("s1", "o1", body) == {"inserted": 1}
    assert not any(p == "/v1/spaces" for _, p, _ in fx.calls)   # no catalog fetch


def test_unknown_non_id_space_segment_warns_without_a_name():
    fx = wire(replies={**SPACES, "/objects/query": {"records": [
        {"id": "o1", "any": {"type": "page"}}]}})
    c = client(fx)
    r = c.put_markdown("s1", "o1", "[f](any://f/nope/fid)")
    assert r["warnings"] == [
        'any://f/nope/fid: "nope" is not a space id — a typed link is any://f/<spaceId>/…']


def test_chat_send_warns_on_text_and_attachment_links_but_still_sends():
    fx = wire(replies={**SPACES, "/messages": {"id": "m1"}})
    c = client(fx)
    r = c.chat_send("s1", "chat1", {
        "text": "here: [x.docx](any://f/ta/2yp2VRDcFqu)",
        "attachments": {"a0": {"type": "link", "link": "any://o/bafyobj1"},
                        "a1": {"type": "link", "link": f"any://o/{SID}/bafyobj1"}}})
    assert r["id"] == "m1"
    assert r["warnings"] == [
        'any://f/ta/2yp2VRDcFqu: "ta" is a space NAME — the space segment '
        f"of a link is its id: any://f/{SID}/2yp2VRDcFqu",
        "any://o/bafyobj1: no space segment — a typed link is any://o/<spaceId>/<id>"]
    sent = next(b for v, p, b in fx.calls if p.endswith("/messages"))
    assert "warnings" not in sent and sent["text"].endswith("2yp2VRDcFqu)")


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
            {"id": "o1", "any": {"name": "Game", "type": "ty_game"}},
            {"id": "o2", "any": {"name": "_soul", "type": "agent_skill"}}]},
        "/types": {"types": [{"id": "ty_game", "name": "Game"},
                             {"id": "agent_skill", "name": "Agent Skill"}]}})
    hits = client(fx).search("s1", "game")["hits"]
    # `type` is the ONE type's display name
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
                                 "type": "bafyTASK"}}]},
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
    c = client(fx, bao="s1")            # the runtime-wired memory home
    assert c.get_brain() == {"objectId": "brain1"}
    c.create_memory({"category": "fact", "context": "x"})
    c.evolve_memory("m1", {"accessCount": 2})
    c.delete_memory("m1")
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
    # every memory call went to the bao space — the only home (ADR-017 §0)
    assert all(p.startswith("/v1/spaces/s1/") for _, p, _ in fx.calls
               if p != "/v1/catalog")


def test_memory_has_one_home():
    # no bao space wired (a run without one): the verbs refuse loudly
    # instead of picking a space; the harness bundle cannot be
    # installed from the guest — that was the model's "repair" that put
    # a brain into a user space (ADR-017 §0)
    fx = wire(replies=_brain_wire())
    c = client(fx)
    with pytest.raises(ValueError, match="no bao space wired"):
        c.get_brain()
    with pytest.raises(ValueError, match="no bao space wired"):
        c.create_memory({"category": "fact", "context": "x"})
    with pytest.raises(ValueError, match="harness bundle"):
        c.ensure_bundle("s2", "bao/v1")
    assert fx.calls == []
    # the module reads the home off the runtime; absent = memory off
    g = load(wire(config={"any.base_url": "http://any", "bao.space": "s1"}))
    assert g["bao_space"]() == "s1"
    g = load(wire(config={"any.base_url": "http://any"}))
    with pytest.raises(ValueError, match="no bao space wired"):
        g["bao_space"]()


def test_memory_validation_is_client_side():
    fx = wire(replies=_brain_wire())
    c = client(fx, bao="s1")
    err = load(wire())["AnyError"]  # class identity differs per load
    with pytest.raises(Exception, match="category required"):
        c.create_memory({"context": "x"})
    with pytest.raises(Exception, match="unknown memory fields"):
        c.create_memory({"category": "fact", "context": "x",
                         "embeddingRef": "nope"})
    with pytest.raises(Exception, match="not evolvable"):
        c.evolve_memory("m1", {"category": "flip"})
    with pytest.raises(Exception, match="confidence"):
        c.create_memory({"category": "fact", "context": "x",
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


# --- collections: tags and supertags (ADR-029 §4) ------------------------------

_COLL = {**_CAT,
         "/collections": {"collections": [
             {"id": "collection", "xKey": "collection", "name": "Collection", "builtIn": True},
             {"id": "miniapp", "xKey": "miniapp", "hidden": True, "builtIn": True},
             {"id": "bin", "xKey": "bin", "hidden": True, "builtIn": True},
             {"id": "bafyRL", "name": "Reading list", "xKey": "reading_list"}]},
         "/collections/bafyRL/properties": {"properties": [
             {"id": "pORD", "name": "Order", "xKey": "order", "kind": "number"}],
             "propId": "pNEW"}}


def test_list_types_and_collections_hide_the_meta_and_builtin_rows():
    fx = wire(replies={**_COLL, "/types": {"types": [
        {"id": "any", "xKey": "any", "builtIn": True},
        {"id": "type", "xKey": "type", "builtIn": True},
        {"id": "collection", "xKey": "collection", "builtIn": True},
        {"id": "page", "xKey": "page", "hidden": True, "builtIn": True},
        {"id": "bafyTASK", "name": "Task", "xKey": "task"}]}})
    c = client(fx)
    # types: the meta rows go, the hidden built-in `page` stays (an
    # object can BE it)
    assert [t["id"] for t in c.list_types("s1")] == ["page", "bafyTASK"]
    # collections: the meta row and miniapp / bin go — reachable by
    # name, never offered as tags
    assert [r["id"] for r in c.list_collections("s1")] == ["bafyRL"]


def test_membership_verbs_resolve_handles_and_route():
    fx = wire(replies={**_COLL, "/objects/query": {"records": [
        {"id": "o1", "any": {"type": "page"}}]}})
    c = client(fx)
    assert c.add_to_collection("s1", "o1", "reading_list") == {}
    assert c.remove_from_collection("s1", "o1", "reading_list") == {}
    assert c.set_type("s1", "o1", "task") == {}
    assert c.trash("s1", "o1") == {}
    assert c.restore("s1", "o1") == {}
    assert [(v, p) for v, p, _ in fx.calls if v in ("POST", "DELETE")] == [
        ("POST", "/v1/spaces/s1/properties/o1/collections/bafyRL"),
        ("DELETE", "/v1/spaces/s1/properties/o1/collections/bafyRL"),
        ("POST", "/v1/spaces/s1/properties/o1/type/bafyTASK"),
        ("POST", "/v1/spaces/s1/properties/o1/collections/bin"),
        ("DELETE", "/v1/spaces/s1/properties/o1/collections/bin")]
    # the wrong kind in a slot is refused with the verb that fits
    with pytest.raises(ValueError, match="is a type, not a collection"):
        c.add_to_collection("s1", "o1", "task")
    with pytest.raises(ValueError, match="is a collection, not a type"):
        c.set_type("s1", "o1", "reading_list")
    with pytest.raises(ValueError, match='collection "ghost" doesn.t exist.*reading_list'):
        c.add_to_collection("s1", "o1", "ghost")


def test_collection_membership_filters_and_column_reads():
    # `any.collections` takes collection xKeys (membership: scalar,
    # $in / $nin / $all); a collection's columns read under its xKey
    fx = wire(replies={**_COLL, "/objects/query": {"records": [
        {"id": "o1", "any": {"name": "Dune", "type": "bafyTASK",
                             "collections": ["bafyRL", "bin"]},
         "bafyRL": {"pORD": 1}}]}})
    c = client(fx)
    [rec] = c.query_objects("s1", filter={"any.collections": "reading_list",
                                          "reading_list.order": {"$lte": 3}},
                            sort=["reading_list.order"])
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"any.collections": "bafyRL", "bafyRL.pORD": {"$lte": 3}}
    assert body["sort"] == ["bafyRL.pORD"]
    assert rec == {"id": "o1", "any": {"name": "Dune", "type": "task",
                                       "collections": ["reading_list", "bin"]},
                   "reading_list": {"order": 1}}
    c.query_objects("s1", filter={"any.collections": {"$nin": ["bin"]},
                                  "any.type": {"$in": ["task", "page"]}})
    body = fx.calls[-1][2]
    assert body["filter"] == {"any.collections": {"$nin": ["bin"]},
                              "any.type": {"$in": ["bafyTASK", "page"]}}


def test_collection_columns_write_through_the_owner_routes():
    # a supertag's column: written under the collection's group on an
    # object, defined through the collection's own property route
    fx = wire(replies={**_COLL, "/objects/query": {"records": [
        {"id": "o1", "any": {"type": "bafyTASK", "collections": ["bafyRL"]}}]}})
    c = client(fx)
    c.update_object("s1", "o1", {"reading_list": {"order": 2}})
    assert ("POST", "/v1/spaces/s1/properties/o1/set/bafyRL",
            {"patch": {"pORD": 2}}) in fx.calls
    c.add_property("s1", "reading_list", {"name": "Note"})
    assert fx.calls[-1][:2] == ("POST", "/v1/spaces/s1/collections/bafyRL/properties")
    assert [r["handle"] for r in c.list_properties("s1", "reading_list")] == ["order"]
    c.patch_property("s1", "reading_list", "order", set={"name": "Rank"})
    assert fx.calls[-1][:2] == ("PATCH", "/v1/spaces/s1/collections/bafyRL/properties/pORD")
    c.delete_property("s1", "reading_list", "order")
    assert fx.calls[-1][:2] == ("DELETE", "/v1/spaces/s1/collections/bafyRL/properties/pORD")


def test_create_collection_is_the_tag_composite():
    fx = wire(replies={**_COLL, "/collections": {"collections": [], "collectionId": "cNEW"},
                       "/collections/cNEW/properties": {"properties": [], "propId": "pX"},
                       "/types/any/properties": {}})
    c = client(fx)
    r = c.create_collection("s1", {"name": "Reading list",
                                   "properties": [{"name": "Order", "kind": "number"}]})
    assert r == {"collectionId": "cNEW", "xKey": "reading_list", "created": True,
                 "addedProps": {"order": "pX"}}
    posts = [(p, b) for v, p, b in fx.calls if v == "POST"]
    assert posts[0] == ("/v1/spaces/s1/collections", {"name": "Reading list",
                                                      "xKey": "reading_list"})
    assert posts[1][0] == "/v1/spaces/s1/collections/cNEW/properties"
    # no layout, no body part: a collection has no behaviour
    assert not any(p.endswith("/parts") for _, p, _ in fx.calls)
    # one handle namespace: a TYPE's handle is refused on this surface,
    # and a collection's on create_type — each naming the other verb
    fx = wire(replies=_COLL)
    c = client(fx)
    with pytest.raises(ValueError, match='already the handle of the type "Task".*set_type'):
        c.create_collection("s1", {"name": "Task"})
    with pytest.raises(ValueError, match='already the handle of the collection.*add_to_collection'):
        c.create_type("s1", {"name": "Reading list"})
    with pytest.raises(ValueError, match="builtin collection"):
        c.create_collection("s1", {"name": "Bin"})
    assert not [p for v, p, _ in fx.calls if v == "POST"]
    # ensure: an existing collection is reused, missing columns added
    fx = wire(replies=_COLL)
    r = client(fx).create_collection("s1", {"name": "Reading list", "properties": [
        {"name": "Order"}, {"name": "Note"}]})
    assert r == {"collectionId": "bafyRL", "xKey": "reading_list", "created": False,
                 "addedProps": {"note": "pNEW"}}


def test_hydrated_link_stubs_carry_type_and_collections():
    cat = {**_COLL, "/types/bafyTASK/properties": {"properties": [
        {"id": "pREL", "name": "Related", "xKey": "related",
         "xFormat": {"type": "relation", "config": {"multiple": True}}}]}}
    rows = {"/objects/query": {"records": [
        {"id": "o1", "any": {"type": "bafyTASK"}, "bafyTASK": {"pREL": ["any://o2"]}},
        {"id": "o2", "any": {"name": "Dune", "type": "page", "collections": ["bafyRL"]}}]}}
    fx = wire(replies={**cat, **rows})
    recs = client(fx).query_objects("s1", filter={"any.type": "task"})
    assert recs[0]["task"]["related"] == [
        {"id": "o2", "name": "Dune", "type": "page", "collections": ["reading_list"]}]


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
        {"id": "p1", "any": {"name": "webSearch", "type": "bafyPROG"},
         "bafyPROG": {"bafyNAME": "webSearch", "bafyVER": "v1",
                      "bafyTOOL": True,
                      "bafySUM": "Web search one-liner."}},
        {"id": "p2", "any": {"name": "helper", "type": "bafyPROG"},
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
    # filter minus binned objects; a definition's row never matches its
    # members (no marker clause needed — ADR-029 §2)
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"$and": [{"any.type": "bafyPROG"},
                                       {"any.collections": {"$nin": ["bin"]}}]}
    # one round-trip per listing — no per-program dataset reads
    assert not [p for v, p, b in fx.calls if p.endswith("/query")
                and not p.endswith("/objects/query")]


def test_list_programs_tools_only_filters():
    fx = wire(replies=dict(_PROG_REPLIES))
    rows = client(fx).list_programs("repo1", tools_only=True)
    assert [r["name"] for r in rows] == ["webSearch"]


# --- flat module surface (ADR-010 §8) ------------------------------------------

def test_list_devices_is_the_registry_read_with_row_flags():
    # ADR-015 §5: the guest reads the registry — self, the
    # server-computed winners, every registered device — and each row
    # is flagged self / active / bao so bao never joins peer ids by hand
    reg = {"self": "p1", "active": {"bao": "p2"},
           "devices": [{"peerId": "p1", "name": "laptop", "os": "linux", "apps": {"bao": {}}},
                       {"peerId": "p2", "name": "mac", "os": "darwin", "apps": {"bao": {}},
                        "activeClaims": {"bao": {"seq": 3, "at": 1}}},
                       {"peerId": "p3", "name": "phone", "os": "ios", "apps": {}}]}
    fx = wire(replies={"/v1/devices": reg})
    out = client(fx).list_devices()
    assert (out["self"], out["active"]) == ("p1", {"bao": "p2"})
    assert [(r["name"], r["self"], r["active"], r["bao"]) for r in out["devices"]] == [
        ("laptop", True, False, True),
        ("mac", False, True, True),
        ("phone", False, False, False),
    ]
    assert out["devices"][1]["activeClaims"] == {"bao": {"seq": 3, "at": 1}}  # raw fields stay
    assert fx.calls == [("GET", "/v1/devices", None)]


def test_list_devices_flags_survive_a_registry_without_a_claim():
    # no claim holder at all (fresh account, or the winner's row deleted)
    reg = {"self": "p1", "active": {}, "devices": [{"peerId": "p1", "apps": {"bao": {}}}]}
    out = client(wire(replies={"/v1/devices": reg})).list_devices()
    assert out["devices"] == [{"peerId": "p1", "apps": {"bao": {}},
                               "self": True, "active": False, "bao": True}]


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
    assert "envelope" in g["search"].__doc__       # ONE authored copy, exported
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
    with pytest.raises(ValueError, match='"nav" is neither a type nor a collection'):
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
    g = load(wire(config={"any.base_url": "http://any"}))
    with pytest.raises(TypeError, match="any.type"):
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
    "/objects/query": {"records": [{"id": "log1", "any": {"type": "lg"}}]},
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
    # with the kind it implies (ADR-027 §4); hidden/layout ride the
    # type create
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
        "/collections": {"collections": [{"id": "miniapp", "xKey": "miniapp",
                                          "hidden": True, "builtIn": True}]},
        "/objects/query": {"records": [
            {"id": "wk", "any": {"name": "Wiki", "type": "__type__", "collections": ["miniapp"]},
             "miniapp": {"bundle": "system:wiki/v1", "pos": "a0"}},
            {"id": "ch", "any": {"name": "General", "description": "Team talk",
                                 "type": "__type__", "collections": ["miniapp"]},
             "miniapp": {"bundle": "system:general-chat/v1", "hidden": True}},
            {"id": "nb", "any": {"name": "Notebook", "type": "page", "collections": ["miniapp"]},
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
    # the sidebar is a COLLECTION's membership (ADR-029 §6)
    q = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert q == {"filter": {"$and": [{"any.collections": "miniapp"},
                                     {"any.collections": {"$nin": ["bin"]}}]},
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
         "properties": {"stage": "pS", "amount": "pA"}}]},
        "/spaces/s1/types": {"types": [{"id": "r2", "xKey": "deal", "name": "Deal"}]}})
    assert client(fx).setup_app("s1", "crm") == [
        {"usecase": "contacts", "bundleId": "system:contacts/v1", "rootId": "r1",
         "installed": False},
        {"usecase": "crm", "bundleId": "system:deal/v1", "rootId": "r2",
         "installed": True, "typeId": "r2", "xKey": "deal",
         "properties": {"stage": "pS", "amount": "pA"}}]
    # the install, then one fresh catalog read for the definitions' xKeys
    assert fx.calls[0] == ("POST", "/v1/catalog/crm/setup", {"spaceId": "s1"})
    assert [p for _, p, _ in fx.calls[1:]] == ["/v1/spaces/s1/types",
                                               "/v1/spaces/s1/collections"]


# --- get_skill: on-demand skill bodies by name (ADR-009 §3) -----------------

def _skill_client(skills, aliases):
    """A _Client whose skill reads come from `skills` {space: [(id, name,
    body)]}; `overlays.aliases` served through runtime.get."""
    c = client(wire(config={"overlays.aliases": aliases}), bao="bao")
    c.list_types = lambda space: ([{"id": "t", "xKey": "agent_skill"}]
                                  if space in skills else [])
    c.query_objects = lambda space, filter=None, **kw: [
        {"id": i, "any": {"name": n}} for i, n, _ in skills.get(space, [])]
    c.get_markdown = lambda space, oid: next(b for i, _, b in skills[space] if i == oid)
    return c


def test_get_skill_looks_in_bao_then_agent_then_connectors():
    c = _skill_client({"bao": [("b1", "review-pr", "# mine"), ("b2", "files", "  \n")],
                       "ag": [("a1", "review-pr", "# shipped"), ("a2", "files", "# files")],
                       "cn": [("c1", "files", "# conn files"), ("c2", "crm", "# crm")]},
                      {"agent": "ag", "connectors": "cn"})
    assert c.get_skill("review-pr") == "# mine"     # the user's shadows the shipped
    assert c.get_skill("files") == "# files"        # a blank body never shadows
    assert c.get_skill("crm") == "# crm"            # connectors last


def test_get_skill_unknown_lists_the_known_ones():
    c = _skill_client({"ag": [("a1", "files", "# f"), ("a0", "_core", "# c")],
                       "cn": [("c2", "crm", "# crm")]}, {"agent": "ag", "connectors": "cn"})
    with pytest.raises(LookupError, match=r"no skill named 'mail'; known: crm, files"):
        c.get_skill("mail")


def test_get_skill_without_overlays_reads_the_bao_space():
    c = _skill_client({"bao": [("b1", "notes", "# n")]}, {})
    assert c.get_skill("notes") == "# n"


def test_get_skill_is_on_the_flat_surface_unscoped():
    g = load(wire())
    assert "get_skill" in g and g["get_skill"].__any_listed__
    assert str(inspect.signature(g["get_skill"])) == "(name)"


def _creating_client(existing=(), with_type=True):
    c = _skill_client({"bao": list(existing)} if with_type else {}, {})
    c.created_types, c.created = [], []
    c.create_type = lambda space, body: c.created_types.append((space, body)) or {}
    c.create_object = lambda space, body, *a, **kw: c.created.append((space, body)) or {
        "objectId": "new1"}
    return c


def test_create_skill_saves_in_the_bao_space_with_its_line():
    c = _creating_client()
    assert c.create_skill("review-pr", "# Skill: review-pr\n\nSteps.", "When a PR needs review.") \
        == {"objectId": "new1"}
    (space, body), = c.created
    assert space == "bao" and body["type"] == "agent_skill"
    assert body["markdown"].startswith("# Skill")
    assert body["initialProperties"] == {"any": {"name": "review-pr",
                                                 "description": "When a PR needs review."},
                                         "agent_skill": {"name": "review-pr"}}
    assert c.created_types == []                     # the type exists: not re-minted


def test_create_skill_mints_the_type_deploy_mints_when_missing():
    c = _creating_client(with_type=False)
    c.create_skill("plan-week", "steps")
    (space, body), = c.created_types
    assert space == "bao" and body["xKey"] == "agent_skill"
    assert body["properties"] == [{"name": "Name", "xKey": "name", "kind": "string"}]


def test_create_skill_refuses_system_names_and_duplicates():
    c = _creating_client(existing=[("b1", "review-pr", "# mine")])
    with pytest.raises(ValueError, match="leading '_'"):
        c.create_skill("_core", "x")
    with pytest.raises(ValueError, match="already have a skill named 'review-pr'"):
        c.create_skill("review-pr", "x")
    assert c.created == []


# --- chat_send guard: the chat the loop answers in -------------------------

def test_chat_send_refuses_a_final_post_into_the_answering_chat():
    # the loop posts the reply itself; a model chat_send there duplicates it
    fx = wire(replies={"/chat/messages": {"recordIds": ["m1"]}},
              config={"any.base_url": "http://any", "bao.space": None})
    g = load(fx)
    g["_answering_in"]("s1", "chat1")
    c = g["_Client"]("http://any", None)
    with pytest.raises(ValueError, match="the chat you are answering in"):
        c.chat_send("s1", "chat1", {"text": "Hi.", "agent": {"name": "bao", "done": True}})
    with pytest.raises(ValueError):
        c.chat_send("s1", "chat1", {"text": "Hi."})
    # progress bubbles and other chats go through; so does the loop's own path
    c.chat_send("s1", "chat1", {"text": "working…", "agent": {"name": "bao", "done": False}})
    c.chat_send("s2", "chat9", {"text": "Watering at 6", "agent": {"name": "bao", "done": True}})
    g["_post_reply"]("s1", "chat1", {"text": "Hi.", "agent": {"name": "bao", "done": True}})
    assert [p for _, p, _ in fx.calls].count("/v1/spaces/s1/objects/chat1/chat/messages") == 2
