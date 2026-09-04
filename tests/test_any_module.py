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
    fx = wire()
    client(fx).upsert_record("s1", "obj1", "agent_triggers", "t1", {"k": "v"})
    assert fx.calls == [("POST", "/v1/spaces/s1/modify", {
        "objectId": "obj1", "dataset": "agent_triggers",
        "records": [{"id": "t1", "upsert": True,
                     "ops": [{"type": "$set", "path": "", "value": {"k": "v"}}]}]})]


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
        ("GET", "/v1/spaces/s1/types/t1/properties"),   # meta.pos append (ADR-022 §4)
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
    # each property appended to the display order (meta.pos, ADR-022 §4)
    assert posts[1][1] == {"name": "Author", "xKey": "author", "kind": "string",
                           "meta": {"pos": "a0"}}
    assert posts[2][1] == {"name": "year", "xKey": "year", "kind": "number",
                           "meta": {"pos": "a0"}}   # fake lists no props → a0


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


def test_create_type_rekeys_legacy_xkeyless_type_by_name():
    # a type from before the server's meta-type xkey move lists with no
    # xKey: create_type re-claims the handle in place (one type.xkey
    # write) and reuses the type — no duplicate
    fx = wire(replies={
        "/types": {"types": [{"id": "t7", "name": "Old Widget"}]},
        "/types/t7/properties": {"properties": [], "propId": "p1"}})
    r = client(fx).create_type("s1", {
        "name": "Old Widget", "properties": [{"name": "Note"}]})
    assert r == {"typeId": "t7", "xKey": "old_widget", "created": False,
                 "addedProps": {"note": "p1"}}
    posts = [(p, b) for v, p, b in fx.calls if v == "POST"]
    assert posts[0] == ("/v1/spaces/s1/properties/t7/set/type",
                        {"patch": {"xkey": "old_widget"}})
    assert not any(p == "/v1/spaces/s1/types" for p, _ in posts)


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
                               "kind": "string", "meta": {"pos": "a0"}}


# --- xKey normalization (ADR-006 §6) -------------------------------------------

# A catalog with one user type `task` (CID id) + builtin `nav` (id == xKey).
# Builtin groups carry their own property catalogs (mirrors the server):
# filter paths under any/nav/program resolve against them (A19).
_ANY_PROPS = {"properties": [
    {"id": "id", "kind": "string", "scope": "derived"},
    {"id": "createdAt", "kind": "datetime", "scope": "derived"},
    {"id": "name", "name": "Name", "kind": "string", "scope": "synced"},
    {"id": "description", "kind": "string", "scope": "synced"},
    {"id": "types", "kind": "array", "scope": "synced"}]}
_CAT = {
    "/types": {"types": [
        {"id": "bafyTASK", "name": "Task", "xKey": "task"},
        {"id": "nav", "name": "Nav", "xKey": "nav"}]},
    "/types/any/properties": _ANY_PROPS,
    "/types/nav/properties": {"properties": [
        {"id": "parentId"}, {"id": "pos"}, {"id": "type"}]},
    "/types/bafyTASK/properties": {"properties": [
        {"id": "bafySTATUS", "name": "Status", "xKey": "status"},
        {"id": "bafyPRIO", "name": "Priority", "xKey": "priority"}]}}


def test_query_objects_normalizes_user_groups_keeps_builtins():
    fx = wire(replies={**_CAT, "/objects/query": {"records": [
        {"id": "o1", "any": {"name": "Ship", "types": ["bafyTASK"]},
         "nav": {"parentId": "f1"},
         "bafyTASK": {"bafySTATUS": "open", "bafyPRIO": 3}}]}})
    [rec] = client(fx).query_objects("s1", filter={"any.types": "task"})
    # user group + its props rekeyed to xKeys; any.types VALUES too;
    # other builtins (nav/id) verbatim
    assert rec == {"id": "o1", "any": {"name": "Ship", "types": ["task"]},
                   "nav": {"parentId": "f1"},
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


def test_get_space_and_general_chat():
    # the chat is the general-chat/v1 bundle's winning root (ADR-017);
    # the bundle id's slash is percent-encoded in the path
    fx = wire(replies={"/spaces/s1": {"id": "s1"},
                       "/bundles/general-chat%2Fv1": {
                           "bundle": {"id": "general-chat/v1", "rootId": "chat9",
                                      "roots": ["old", "chat9"], "derived": True},
                           "synced": True}})
    c = client(fx)
    assert c.get_space("s1")["id"] == "s1"
    # the locked-read envelope {bundle, synced} is unwrapped to the row
    row = c.get_bundle("s1", "general-chat/v1")
    assert row["rootId"] == "chat9" and row["synced"] is True
    assert c.general_chat("s1") == "chat9"
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1"),
        ("GET", "/v1/spaces/s1/bundles/general-chat%2Fv1"),
        ("GET", "/v1/spaces/s1/bundles/general-chat%2Fv1")]


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
    # one call creates a page: the wire takes no body, so markdown
    # (alias `body`) rides as a put_markdown right after the create
    fx = wire(replies={**_CAT, "/objects": {"objectId": "o9"},
                       "/editor/markdown": {}})
    client(fx).create_object("s1", {"types": ["task"], "name": "Dune",
                                    "markdown": "# Dune\n\nsand"})
    paths = [(v, p) for v, p, _ in fx.calls]
    i_create = paths.index(("POST", "/v1/spaces/s1/objects"))
    i_md = paths.index(("PUT", "/v1/spaces/s1/objects/o9/editor/markdown"))
    assert i_create < i_md
    body = next(b for v, p, b in fx.calls if p == "/v1/spaces/s1/objects")
    assert "markdown" not in body
    md = next(b for v, p, b in fx.calls if p.endswith("/editor/markdown"))
    assert md["content"] == "# Dune\n\nsand"


def test_create_object_unknown_top_level_key_raises_never_posts():
    # the wire accepts only types/initialProperties/nav and silently
    # drops the rest — the client refuses instead of losing intent
    fx = wire(replies=_CAT)
    with pytest.raises(ValueError, match="unknown top-level key"):
        client(fx).create_object("s1", {"types": ["task"],
                                        "any": {"name": "x"}})
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
    fx = wire(replies=_CAT)
    r = client(fx).update_object("s1", "o1", {
        "name": "Renamed", "description": "now with mangoes",
        "markdown": "# body", "task": {"status": "done"}})
    assert r == {"objectId": "o1"}
    posts = [(p, b) for v, p, b in fx.calls if v in ("POST", "PUT")]
    assert ("/v1/spaces/s1/objects/o1/editor/markdown", {"content": "# body"}) in posts
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
        path = payload["url"].removeprefix("http://any")
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
            {"id": "d1", "name": "email_messages",
             "search": {"title": "subject", "text": "body",
                        "scope": "email"}}]},
        "/types": {"types": [{"id": "mb", "xKey": "mailbox"}]},
    })
    r = client(fx).create_dataset("s1", "mailbox", {
        "name": "email_messages",
        "search": {"title": "subject", "text": ["body", "notes"],
                   "scope": "email"}})
    assert r == {"datasetDefId": "d1", "created": False,
                 "patched": ["search.text"]}
    verb, path, body = next(c for c in fx.calls if c[0] == "PATCH")
    assert path.endswith("/types/mb/datasets/d1")
    assert body == {"set": {"search.text": ["body", "notes"]}}


def test_create_dataset_single_element_text_array_is_not_drift():
    # The server stores a one-key array as the bare string; a draft
    # saying ["body"] against a stored "body" must NOT patch
    fx = wire(replies={
        "/types/mb/datasets": {"datasets": [
            {"id": "d1", "name": "email_messages",
             "search": {"title": "subject", "text": "body",
                        "scope": "email"}}]},
        "/types": {"types": [{"id": "mb", "xKey": "mailbox"}]},
    })
    r = client(fx).create_dataset("s1", "mailbox", {
        "name": "email_messages",
        "search": {"title": "subject", "text": ["body"],
                   "scope": "email"}})
    assert r == {"datasetDefId": "d1", "created": False}
    assert not [c for c in fx.calls if c[0] == "PATCH"]


def test_turns_chunks_chat_paths():
    # ADR-017: turns/chunks land on the chat's log child (bao/log/v1 of
    # the chat's bundle) via upsert with a client-assigned seq; the
    # agent_log store is ensured lazily (type + datasets pre-exist in
    # this fixture, search leaves matching so nothing is patched)
    fx = wire(replies={
        "/types/lg/datasets": {"datasets": [
            {"id": "d1", "name": "agent_turns",
             "search": {"title": "userText", "text": "searchText",
                        "scope": "history"}},
            {"id": "d2", "name": "agent_chunks",
             "search": {"text": "summary", "scope": "history"}}]},
        "/types": {"types": [{"id": "lg", "xKey": "agent_log"}]},
        "/children": {"objectId": "log1"},
        "/bundles": {"bundles": [{"id": "general-chat/v1",
                                  "rootId": "chat1"}]},
        # the highest id ever written is a TOMBSTONE (content wiped, no
        # seq) — the allocator must still count past it (ADR-017 §2)
        "/query": {"records": [{"id": "00000004",
                                "_deletedAt": {"$date": "2026-09-01T00:00:00Z"}}]},
        "/upsert": {"created": 1, "updated": 0, "skipped": 0},
    })
    c = client(fx)
    r = c.append_turn("s1", "chat1", {"userText": "hi", "replies": ["yo"]})
    assert r == {"recordIds": ["00000005"], "seq": 5}
    probe = next(b for v, p, b in fx.calls if p.endswith("/query")
                 and b.get("dataset") == "agent_turns")
    assert probe == {"objectId": "log1", "dataset": "agent_turns",
                     "includeDeleted": True, "sort": ["-id"], "limit": 1}
    c.create_chunk("s1", "chat1", {"level": 1})
    c.chat_send("s1", "chat1", {"text": "yo"})
    turn_up = next(b for v, p, b in fx.calls
                   if p.endswith("/upsert")
                   and b["dataset"] == "agent_turns")
    assert turn_up["objectId"] == "log1"
    rec = turn_up["records"][0]
    assert rec["id"] == "00000005"
    assert rec["fields"]["seq"] == 5
    assert rec["fields"]["searchText"] == "hi yo"
    chunk_up = next(b for v, p, b in fx.calls
                    if p.endswith("/upsert")
                    and b["dataset"] == "agent_chunks")
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
    # display order: no meta.pos → by name; handle = the slug xKey
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


def test_add_property_format_leaves_kind_to_server():
    # with a format the server derives kind (links⇒array, date⇒string) —
    # the helper must NOT inject its "string" default
    fx = wire(replies={**_CAT, "/types/bafyTASK/properties": {"propId": "p9"}})
    client(fx).add_property("s1", "task", {"name": "Due Date",
                                           "format": {"type": "date"}})
    body = fx.calls[-1][2]
    assert "kind" not in body
    assert body["format"] == {"type": "date"}


def test_aggregate_speaks_xkeys_in_records():
    fx = wire(replies={**_CAT, "/objects/aggregate": {"records": [
        {"id": ["bafyTASK", "nav"], "count": 2},
        {"id": ["chat"], "count": 1}]}})
    r = client(fx).aggregate("s1", [{"$group": {"_id": "$any.types",
                                                "count": {"$sum": 1}}}])
    assert r["records"] == [{"id": ["task", "nav"], "count": 2},
                            {"id": ["chat"], "count": 1}]


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
    fx = wire(replies={**_CAT, "/objects/o1/backlinks": {"backlinks": [
        {"objectId": "src", "typeId": "bafyTASK", "propId": "bafySTATUS"}]}})
    assert client(fx).backlinks("s1", "o1") == [
        {"objectId": "src", "type": "task", "prop": "status"}]


# --- markdown ---------------------------------------------------------------------

def test_edit_markdown_wire_shape():
    fx = wire(replies={"/editor/markdown": {"updated": 1, "unchanged": 4}})
    r = client(fx).edit_markdown("s1", "o1", [
        {"oldText": "- [ ] Buy milk", "newText": "- [x] Buy milk"}])
    verb, path, body = fx.calls[-1]
    assert (verb, path) == ("PATCH",
                            "/v1/spaces/s1/objects/o1/editor/markdown")
    assert body == {"edits": [{"oldText": "- [ ] Buy milk",
                               "newText": "- [x] Buy milk"}]}
    assert r == {"updated": 1, "unchanged": 4}


def test_markdown_roundtrip_uses_content_key():
    fx = wire(replies={"/editor/markdown": {"content": "# hi"}})
    c = client(fx)
    assert c.get_markdown("s1", "o1") == "# hi"
    c.put_markdown("s1", "o1", "# bye")
    c.append_markdown("s1", "o1", "\n## more")
    assert fx.calls == [
        ("GET", "/v1/spaces/s1/objects/o1/editor/markdown", None),
        ("PUT", "/v1/spaces/s1/objects/o1/editor/markdown", {"content": "# bye"}),
        ("POST", "/v1/spaces/s1/objects/o1/editor/markdown/append",
         {"content": "\n## more"})]


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
            {"id": "o1", "any": {"name": "Game", "types": ["ty_game", "nav", "editor"]}},
            {"id": "o2", "any": {"name": "_soul", "types": ["agent_skill", "nav"]}}]},
        "/types": {"types": [{"id": "ty_game", "name": "Game"},
                             {"id": "agent_skill", "name": "Agent Skill"}]}})
    hits = client(fx).search("s1", "game")["hits"]
    assert (hits[0]["title"], hits[0]["type"]) == ("Game", "Game")
    assert (hits[1]["title"], hits[1]["type"]) == ("_soul", "Agent Skill")
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
                                 "types": ["bafyTASK", "nav"]}}]},
        **_CAT})
    hits = client(fx).search("s1", "open")["hits"]
    assert hits[0]["prop"] == "task.status"   # propId resolved via the catalog
    assert "prop" not in hits[1]              # builtin name recordId untouched


def test_search_enrich_false_skips_the_extra_query():
    fx = wire(replies={"/search": {"hits": [{"objectId": "o1"}]}})
    client(fx).search("s1", "q", enrich=False)
    assert [p for v, p, _ in fx.calls] == ["/v1/spaces/s1/search"]


def test_backlinks_unwraps_and_null_degrades_to_empty():
    fx = wire(replies={"/backlinks": {"backlinks": None}})
    assert client(fx).backlinks("s1", "o1") == []
    assert fx.calls == [("GET", "/v1/spaces/s1/objects/o1/backlinks", None)]


# --- agent memory ---------------------------------------------------------------------

def _brain_wire():
    # agent_brain store pre-exists (type + datasets with matching
    # search leaves), brain child derives to brain1
    return {
        "/types/br/datasets": {"datasets": [
            {"id": "d1", "name": "agent_memory_items",
             "search": {"title": "context", "text": "body",
                        "scope": "agent"}},
            {"id": "d2", "name": "agent_job_state"}]},
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
    assert create["dataset"] == "agent_memory_items"
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
    # space create is a this-side-installs case (ADR-006 §0): the POST
    # is followed by the derived general-chat/v1 ensure, and the reply
    # is the trimmed row + the chat id (no chat id rides the space row)
    fx = wire(replies={
        "/v1/spaces": {"id": "sp9", "name": "AI Startups",
                       "push": {"encKey": "SECRET"}},
        "/bundles": {"bundle": {"id": "general-chat/v1", "rootId": "chat9",
                                "derived": True}, "installed": True}})
    r = client(fx).create_space("AI Startups")
    assert r == {"id": "sp9", "name": "AI Startups", "generalChatId": "chat9"}
    # no spaceType: empty = server default on every vintage
    # ("anytype.space" is rejected since SDK v0.0.10)
    assert fx.calls == [
        ("POST", "/v1/spaces", {"name": "AI Startups"}),
        ("POST", "/v1/spaces/sp9/bundles",
         {"id": "general-chat/v1", "name": "General", "rootTypes": ["chat"],
          "derived": True}),
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
    # the object query targeted the requested space with the program filter
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"any.types": "bafyPROG"}
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
    with pytest.raises(ValueError, match='"parentid" on builtin group "nav"'):
        client(fx).query_objects("s1", filter={"nav.parentid": "f1"})
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


def test_dataset_query_passes_instants_and_unknown_keys():
    fx = wire(replies={"/query": {"records": []}})
    c = client(fx)
    lit = {"$date": 1787673600000}
    c.query("s1", "log1", "agent_turns",
            filter={"createdAt": {"$gte": lit, "$lte": {"$date": "2026-09-01T00:00:00Z"}},
                    "seq": {"$gt": 3}, "lastModifiedAt": {"$gt": 7}})
    assert fx.calls[-1][2]["filter"]["createdAt"]["$gte"] == lit
    c.query("s1", "log1", "agent_turns", filter={"createdAt": {"$exists": True}})
    c.query("s1", "log1", "agent_turns", filter={"createdAt": {"$in": [lit]}})


def test_create_dataset_draft_registers_its_datetime_keys():
    fx = wire(replies={**_CAT, "/types/bafyTASK/datasets": {"datasets": []},
                       "/query": {"records": []}})
    c = client(fx)
    c.create_dataset("s1", "task", {"name": "events", "idRule": "user",
                                    "deleteBy": "anyone",
                                    "fields": [{"key": "at", "kind": "datetime"}]})
    with pytest.raises(ValueError, match='"at"'):
        c.query("s1", "o1", "events", filter={"at": {"$lt": 1}})


def test_objects_query_refuses_bare_literal_on_stamps_and_datetime_props():
    cat = {**_CAT, "/types/bafyTASK/properties": {"properties": [
        {"id": "bafyDUE", "name": "Due", "xKey": "due", "kind": "datetime",
         "format": {"type": "date"}},
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


def test_create_type_posts_property_formats_without_a_kind():
    # ADR-019 §5: a date-format property declared through create_type
    # reaches the wire with its format and NO kind (server derives
    # datetime) — the format used to be dropped on this path
    fx = wire(replies={**_CAT, "/types": {"types": [], "typeId": "tNew"},
                       "/types/tNew/properties": {"properties": [], "propId": "p1"}})
    client(fx).create_type("s1", {"name": "Event", "properties": [
        {"name": "When", "format": {"type": "datetime"}},
        {"name": "Title"}]})
    posted = [b for v, p, b in fx.calls if v == "POST" and p.endswith("/properties")]
    # + the any-ui kind marker beside the format (ADR-022 §1), so the
    # UI's picker reads the property like one it made itself
    assert posted[0] == {"name": "When", "xKey": "when", "format": {"type": "datetime"},
                         "xKind": "date", "meta": {"pos": "a0"}}
    assert posted[1] == {"name": "Title", "xKey": "title", "kind": "string",
                         "meta": {"pos": "a0"}}


def test_property_url_email_longtext_are_xkind_markers_not_formats():
    # ADR-022 §1: any-ui's client conventions — a string property with
    # an xKind marker and NO format on the wire (the server has no
    # such format and would 400)
    fx = wire(replies={**_CAT, "/types/bafyTASK/properties": {"propId": "p9"}})
    c = client(fx)
    for marker in ("url", "email", "longtext"):
        c.add_property("s1", "task", {"name": marker.title(),
                                      "format": {"type": marker}})
        body = fx.calls[-1][2]
        assert body == {"name": marker.title(), "xKey": marker,
                        "kind": "string", "xKind": marker, "meta": {"pos": "a0"}}
    # an explicit xKind passes through untouched; a non-string kind is refused
    c.add_property("s1", "task", {"name": "Site", "xKind": "url"})
    assert fx.calls[-1][2]["xKind"] == "url"
    with pytest.raises(ValueError, match='kind must be "string"'):
        c.add_property("s1", "task", {"name": "N", "kind": "number",
                                      "format": {"type": "url"}})
    # an unknown format names both vocabularies
    with pytest.raises(ValueError, match="server formats.*client conventions"):
        c.add_property("s1", "task", {"name": "P", "format": {"type": "phone"}})


def test_create_type_stamps_xkind_beside_server_formats():
    fx = wire(replies={**_CAT, "/types": {"types": [], "typeId": "tNew"},
                       "/types/tNew/properties": {"properties": [], "propId": "p1"}})
    client(fx).create_type("s1", {"name": "Bookmark", "properties": [
        {"name": "Link", "format": {"type": "url"}},
        {"name": "Status", "format": {"type": "select",
                                      "options": {"new": "New"}}},
        {"name": "Tags", "format": {"type": "multiselect"}},
        {"name": "Related", "format": {"type": "links"}}]})
    posted = [b for v, p, b in fx.calls if v == "POST" and p.endswith("/properties")]
    assert [(b.get("xKind"), b.get("kind"), (b.get("format") or {}).get("type"))
            for b in posted] == [
        ("url", "string", None), ("select", None, "select"),
        ("tags", None, "multiselect"), ("links", None, "links")]


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
    fx = wire(replies={"/files?objectId=o1": {"files": [{"fileId": "f1", "mime": "text/plain"}]}})
    c = client(fx)
    assert c.list_files("s1", "o1") == [{"fileId": "f1", "mime": "text/plain"}]
    assert fx.calls == [("GET", "/v1/spaces/s1/files?objectId=o1", None)]


def test_file_not_available_carries_sync_hint():
    def fx(name, payload):
        body = json.dumps({"error": {"code": "file.not_available", "message": "no bytes"}})
        return {"status": 409, "headers": {}, "body": body}

    g = load(fx)
    with pytest.raises(g["AnyError"]) as e:
        g["_Client"]("http://any").file_content("s", "f")
    assert "not synced" in str(e.value)


def test_list_search_scopes_unions_fixed_and_declared():
    fx = wire(replies={
        "/v1/spaces/s1/datasets": {"datasets": [
            {"name": "chat_messages", "typeId": "chat"},
            {"name": "email_messages", "typeId": "bafyreimailbox00000000000000"},
            {"name": "agent_turns", "typeId": "bafyreiagentlog0000000000000"}]},
        "bafyreimailbox00000000000000/datasets": {"datasets": [
            {"name": "email_messages",
             "search": {"title": "subject", "text": ["from", "body"], "scope": "email"}}]},
        "bafyreiagentlog0000000000000/datasets": {"datasets": [
            {"name": "agent_turns", "search": {"text": "text", "scope": "history"}},
            {"name": "agent_chunks", "search": {"text": "summary", "scope": "history"}}]}})
    c = client(fx)
    assert c.list_search_scopes("s1") == ["basic", "chat", "email", "history", "props"]
    # builtin datasets (typeId "chat") are not walked — one call per user type
    assert [p for _, p, _ in fx.calls] == [
        "/v1/spaces/s1/datasets",
        "/v1/spaces/s1/types/bafyreiagentlog0000000000000/datasets",
        "/v1/spaces/s1/types/bafyreimailbox00000000000000/datasets"]
