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
    g = {"effect": fx, "span": lambda name=None, kind=None: (lambda f: f), "use": None}
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
    assert posts[1][1] == {"name": "Author", "xKey": "author", "kind": "string"}
    assert posts[2][1] == {"name": "year", "xKey": "year", "kind": "number"}


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
                               "kind": "string"}


# --- xKey normalization (ADR-006 §6) -------------------------------------------

# A catalog with one user type `task` (CID id) + builtin `nav` (id == xKey).
# Builtin groups carry their own property catalogs (mirrors the server):
# filter paths under any/nav/program resolve against them (A19).
_ANY_PROPS = {"properties": [
    {"id": "id", "kind": "string", "scope": "derived"},
    {"id": "createdAt", "kind": "number", "scope": "derived"},
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
                           "id": "general-chat/v1", "rootId": "chat9"}})
    c = client(fx)
    assert c.get_space("s1")["id"] == "s1"
    assert c.general_chat("s1") == "chat9"
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1"),
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


def test_get_ui_context_none_when_type_absent():
    # fresh spaces have no ui_context type — the documented None path
    # must survive strict filter resolution
    fx = wire(replies={"/types": {"types": []}})
    assert client(fx).get_ui_context("s1") is None


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
        if name == "config.get":
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
        "/query": {"records": [{"id": "00000004", "seq": 4}]},
        "/upsert": {"created": 1, "updated": 0, "skipped": 0},
    })
    c = client(fx)
    r = c.append_turn("s1", "chat1", {"userText": "hi", "replies": ["yo"]})
    assert r == {"recordIds": ["00000005"], "seq": 5}
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
    assert c.list_properties("s1", "t1") == [{"id": "p1"}]
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1/types"),
        ("GET", "/v1/spaces/s1/types"),   # list_properties type resolution
        ("GET", "/v1/spaces/s1/types/t1/properties")]


def test_list_properties_takes_xkey_and_errors_on_unknown():
    fx = wire(replies=_CAT)
    c = client(fx)
    # xKey resolves to the CID route — the agent never needs the id
    assert c.list_properties("s1", "task")[0]["xKey"] == "status"
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


def test_create_space_wire_shape_and_full_row_reply():
    fx = wire(replies={"/v1/spaces": {
        "id": "sp9", "name": "AI Startups",
        "generalChatObjectId": "chat9"}})
    r = client(fx).create_space("AI Startups")
    assert r["id"] == "sp9" and r["generalChatObjectId"] == "chat9"
    # no spaceType: empty = server default on every vintage
    # ("anytype.space" is rejected since SDK v0.0.10)
    assert fx.calls == [("POST", "/v1/spaces", {"name": "AI Startups"})]
    # description only rides the wire when given
    client(fx).create_space("x", description="d")
    assert fx.calls[-1][2] == {"name": "x", "description": "d"}


_UI_CTX_TYPES = {
    "/types": {"types": [{"id": "T1", "xKey": "ui_context"}]},
    "/types/any/properties": _ANY_PROPS,
    "/types/T1/properties": {"properties": [
        {"id": "p_s", "xKey": "space_id"},
        {"id": "p_o", "xKey": "object_id"},
        {"id": "p_v", "xKey": "view"},
        {"id": "p_u", "xKey": "updated_at"}]}}


def _ui_ctx_wire(records):
    return wire(replies={**_UI_CTX_TYPES, "/objects/query": {"records": records}})


_TWO_POINTERS = [
    {"id": "o1", "T1": {"p_s": "sp1", "p_o": "ob1", "p_v": "object", "p_u": 111}},
    {"id": "o2", "T1": {"p_s": "sp2", "p_o": "", "p_v": "grid", "p_u": 222}}]


def _deleted(fx):
    return [p.rsplit("/", 1)[-1] for v, p, _ in fx.calls if v == "DELETE"]


def test_get_ui_context_resolves_props_and_picks_newest():
    fx = _ui_ctx_wire(_TWO_POINTERS)
    ctx = client(fx).get_ui_context("s1")
    assert ctx == {"spaceId": "sp2", "objectId": "", "view": "grid",
                   "updatedAt": 222}
    # filters by the any.types xKey (resolved to the type id); records come
    # back xKey-normalized so the pointer props read by their slug
    q = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert q == {"filter": {"any.types": "T1"}, "limit": 50}
    # the getter stays a getter: duplicates are picked from, never pruned
    assert _deleted(fx) == []


def test_get_ui_context_none_when_type_or_pointer_absent():
    fx = wire(replies={"/types": {"types": []}})
    assert client(fx).get_ui_context("s1") is None
    fx = wire(replies={
        "/types": {"types": [{"id": "T1", "xKey": "ui_context"}]},
        "/types/any/properties": _ANY_PROPS,
        "/types/T1/properties": {"properties": []},
        "/objects/query": {"records": []}})
    assert client(fx).get_ui_context("s1") is None


# --- the duplicate-pointer stopgap (last-modified wins, rest deleted) ---------
# TODO with the code: the ui-context protocol rework replaces all of this.

def test_prune_ui_contexts_keeps_newest_and_deletes_the_rest():
    fx = _ui_ctx_wire(_TWO_POINTERS)
    ctx = client(fx)._prune_ui_contexts("s1")
    assert ctx == {"spaceId": "sp2", "objectId": "", "view": "grid",
                   "updatedAt": 222}
    assert _deleted(fx) == ["o1"]      # the stale pointer, and only it


def test_prune_ui_contexts_ranks_by_server_mtime_when_updated_at_ties():
    # pointers written before any-ui carried updated_at (or by a client
    # that never set it): server modifiedAt breaks the tie
    fx = _ui_ctx_wire([
        {"id": "o1", "modifiedAt": 20, "T1": {"p_s": "sp1", "p_v": "grid"}},
        {"id": "o2", "modifiedAt": 10, "T1": {"p_s": "sp2", "p_v": "object"}}])
    ctx = client(fx)._prune_ui_contexts("s1")
    assert ctx["spaceId"] == "sp1" and ctx["updatedAt"] == 0
    assert _deleted(fx) == ["o2"]


def test_prune_ui_contexts_leaves_a_lone_pointer_alone():
    fx = _ui_ctx_wire([_TWO_POINTERS[1]])
    assert client(fx)._prune_ui_contexts("s1")["spaceId"] == "sp2"
    assert _deleted(fx) == []


def test_prune_ui_contexts_none_when_type_or_pointer_absent():
    fx = wire(replies={"/types": {"types": []}})
    assert client(fx)._prune_ui_contexts("s1") is None
    fx = _ui_ctx_wire([])
    assert client(fx)._prune_ui_contexts("s1") is None
    assert _deleted(fx) == []


def test_prune_ui_contexts_survives_a_failed_delete():
    # a 500 on one stale pointer must not cost the run its view line
    ok = _ui_ctx_wire(_TWO_POINTERS)

    def flaky(name, payload):
        reply = ok(name, payload)
        if name == "http.delete":
            return {"status": 500, "headers": {},
                    "body": json.dumps({"error": {"code": "internal",
                                                  "message": "nope"}})}
        return reply

    flaky.calls = ok.calls
    c = load(flaky)["_Client"]("http://any")
    assert c._prune_ui_contexts("s1")["spaceId"] == "sp2"
    assert _deleted(flaky) == ["o1"]   # attempted, failed, run continues


# --- list_programs (ADR-009 §2: repo browsing) --------------------------------

_PROG_REPLIES = {
    "/types": {"types": [{"id": "bafyPROG", "name": "Program", "xKey": "program"}]},
    "/types/any/properties": _ANY_PROPS,
    "/types/program/properties": {"properties": [
        {"id": "name"}, {"id": "version"}, {"id": "any_tool"},
        {"id": "summary"}]},
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


def test_program_group_paths_still_pass():
    # toolcaller's compose filters {"program.any_tool": True} — the
    # builtin-group check must resolve it, never reject it
    fx = wire(replies={**_PROG_REPLIES, "/objects/query": {"records": []}})
    client(fx).query_objects("s1", filter={"program.any_tool": True})
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"program.any_tool": True}


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


def test_get_space_trims_but_keeps_derived_object_ids():
    fx = wire(replies={"/v1/spaces": {"spaces": []},
                       "/spaces/s1": {"id": "s1", "generalChatObjectId": "chat9",
                                      "push": {"encKey": "SECRET"},
                                      "agentConfigObjectId": "cfg1"}},
              config={"any.base_url": "http://any"})
    r = load(fx)["get_space"]("s1")
    assert r == {"id": "s1", "generalChatObjectId": "chat9",
                 "agentConfigObjectId": "cfg1"}


def test_search_types_kwarg_redirects_to_query_objects():
    # A18: the guessed types= kwarg gets the redirect, not a bare TypeError
    g = load(wire())
    with pytest.raises(TypeError, match="any.types"):
        g["search"](SID, "q", types=["task"])


# --- the duplicate-TYPE stopgap (same freshest-wins logic, one level up) ------
# Racing clients on different peers mint duplicate ui_context TYPE defs (the
# xKey 409 guard is per-peer); every xKey→id resolution then picks an arbitrary
# one and the other side's pointer turns invisible. Winner = max (modifiedAt,
# id) over the type's own object row; losers (and pointers not carrying the
# winner) are deleted — type defs are ordinary object rows, so delete_object
# works where Types.Delete is unimplemented. Mirrored in any-ui PR #445.

def _dup_type_fx(type_rows, pointers_by_tid):
    """Two ui_context types; /objects/query answers by FILTER shape."""
    base = wire(replies={
        "/types": {"types": [{"id": "T1", "xKey": "ui_context"},
                             {"id": "T2", "xKey": "ui_context"}]},
        "/types/any/properties": _ANY_PROPS,
        "/types/T1/properties": {"properties": [
            {"id": "p_s", "xKey": "space_id"}, {"id": "p_u", "xKey": "updated_at"}]},
        "/types/T2/properties": {"properties": [
            {"id": "q_s", "xKey": "space_id"}, {"id": "q_u", "xKey": "updated_at"}]}})

    def fx(name, payload):
        path = payload.get("url", "").removeprefix("http://any")
        if name == "http.post" and path.endswith("/objects/query"):
            base.calls.append(("POST", path, payload.get("json")))
            filt = (payload.get("json") or {}).get("filter") or {}
            recs = (type_rows if "id" in filt
                    else pointers_by_tid.get(filt.get("any.types"), []))
            return {"status": 200, "headers": {},
                    "body": json.dumps({"records": recs})}
        return base(name, payload)

    fx.calls = base.calls
    return fx


def test_prune_converges_duplicate_types_on_freshest():
    fx = _dup_type_fx(
        [{"id": "T1", "modifiedAt": 5}, {"id": "T2", "modifiedAt": 9}],
        {"T1": [{"id": "o1", "T1": {"p_s": "sp1", "p_u": 999}}],
         "T2": [{"id": "o2", "T2": {"q_s": "sp2", "q_u": 100}}]})
    ctx = client(fx)._prune_ui_contexts("s1")
    # o1 is the freshest pointer overall, but it rides the LOSER type —
    # the winner-typed o2 survives, o1 and the loser type def go
    assert ctx["spaceId"] == "sp2"
    assert _deleted(fx) == ["o1", "T1"]   # pointers before type defs


def test_prune_deletes_everything_when_no_pointer_carries_the_winner():
    fx = _dup_type_fx(
        [{"id": "T1", "modifiedAt": 5}, {"id": "T2", "modifiedAt": 9}],
        {"T1": [{"id": "o1", "T1": {"p_s": "sp1", "p_u": 999}}], "T2": []})
    assert client(fx)._prune_ui_contexts("s1") is None
    assert _deleted(fx) == ["o1", "T1"]   # UI re-creates on the winner


def test_prune_duplicate_type_tie_breaks_on_id():
    fx = _dup_type_fx(
        [{"id": "T1", "modifiedAt": 5}, {"id": "T2", "modifiedAt": 5}],
        {"T1": [], "T2": [{"id": "o2", "T2": {"q_s": "sp2", "q_u": 1}}]})
    assert client(fx)._prune_ui_contexts("s1")["spaceId"] == "sp2"
    assert _deleted(fx) == ["T1"]         # T2 wins the (mtime, id) tie


def test_get_ui_context_reads_across_duplicate_types_without_deleting():
    # the read path is availability-first: freshest pointer wins even on a
    # loser type, and a pure getter never prunes
    fx = _dup_type_fx(
        [{"id": "T1", "modifiedAt": 5}, {"id": "T2", "modifiedAt": 9}],
        {"T1": [{"id": "o1", "T1": {"p_s": "sp1", "p_u": 999}}],
         "T2": [{"id": "o2", "T2": {"q_s": "sp2", "q_u": 100}}]})
    assert client(fx).get_ui_context("s1")["spaceId"] == "sp1"
    assert _deleted(fx) == []


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
