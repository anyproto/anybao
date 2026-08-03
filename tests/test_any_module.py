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
    g = {"effect": fx, "span": lambda name, kind=None: (lambda f: f), "use": None}
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
    assert "hint" not in str(ei.value)          # no hint for unlisted codes


def test_known_codes_carry_recovery_hint_verbatim_message():
    server_msg = 'unknown field "tokensIn"; accepted fields: seq, llm'
    fx = wire(status=400, replies={
        "": {"error": {"code": "request.unknown_field",
                       "message": server_msg}}})
    g = load(fx)
    with pytest.raises(g["AnyError"]) as ei:
        g["client"]("http://any").query("s", "o", "d")
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


def test_add_property_defaults_xkey_and_kind():
    fx = wire(replies={"/types": {"types": [{"id": "t1"}]},
                       "/types/t1/properties": {"propId": "p1"}})
    client(fx).add_property("s1", "t1", {"name": "Due Date"})
    assert fx.calls[-1][2] == {"name": "Due Date", "xKey": "due_date",
                               "kind": "string"}


# --- xKey normalization (ADR-006 §6) -------------------------------------------

# A catalog with one user type `task` (CID id) + builtin `nav` (id == xKey).
_CAT = {
    "/types": {"types": [
        {"id": "bafyTASK", "name": "Task", "xKey": "task"},
        {"id": "nav", "name": "Nav", "xKey": "nav"}]},
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
    fx = wire(replies={"/spaces/s1": {"id": "s1",
                                      "generalChatObjectId": "chat9"}})
    c = client(fx)
    assert c.get_space("s1")["generalChatObjectId"] == "chat9"
    assert c.general_chat("s1") == "chat9"
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("GET", "/v1/spaces/s1"), ("GET", "/v1/spaces/s1")]


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
        "name": "Renamed", "markdown": "# body",
        "task": {"status": "done"}})
    assert r == {"objectId": "o1"}
    posts = [(p, b) for v, p, b in fx.calls if v in ("POST", "PUT")]
    assert ("/v1/spaces/s1/objects/o1/editor/markdown", {"content": "# body"}) in posts
    # name -> set/any patch; property group -> set/<typeId> patch
    assert ("/v1/spaces/s1/properties/o1/set/any",
            {"patch": {"name": "Renamed"}}) in posts
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

    c = load(fx)["client"]("http://any")
    c.create_object("s1", {"types": ["task"]})   # miss then hit
    assert seen["n"] == 2


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
    assert fx.calls == [("POST", "/v1/spaces",
                         {"name": "AI Startups", "spaceType": "anytype.space"})]
    # description only rides the wire when given
    client(fx).create_space("x", description="d")
    assert fx.calls[-1][2] == {"name": "x", "spaceType": "anytype.space",
                               "description": "d"}


def test_get_ui_context_resolves_props_and_picks_newest():
    fx = wire(replies={
        "/types": {"types": [{"id": "T1", "xKey": "ui_context"}]},
        "/types/T1/properties": {"properties": [
            {"id": "p_s", "xKey": "space_id"},
            {"id": "p_o", "xKey": "object_id"},
            {"id": "p_v", "xKey": "view"},
            {"id": "p_u", "xKey": "updated_at"}]},
        "/objects/query": {"records": [
            {"id": "o1", "T1": {"p_s": "sp1", "p_o": "ob1",
                                "p_v": "object", "p_u": 111}},
            {"id": "o2", "T1": {"p_s": "sp2", "p_o": "",
                                "p_v": "grid", "p_u": 222}}]}})
    ctx = client(fx).get_ui_context("s1")
    assert ctx == {"spaceId": "sp2", "objectId": "", "view": "grid",
                   "updatedAt": 222}
    # filters by the any.types xKey (resolved to the type id); records come
    # back xKey-normalized so the pointer props read by their slug
    q = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert q == {"filter": {"any.types": "T1"}, "limit": 8}


def test_get_ui_context_none_when_type_or_pointer_absent():
    fx = wire(replies={"/types": {"types": []}})
    assert client(fx).get_ui_context("s1") is None
    fx = wire(replies={
        "/types": {"types": [{"id": "T1", "xKey": "ui_context"}]},
        "/types/T1/properties": {"properties": []},
        "/objects/query": {"records": []}})
    assert client(fx).get_ui_context("s1") is None


# --- list_programs (ADR-009 §2: repo browsing) --------------------------------

_PROG_REPLIES = {
    "/types": {"types": [{"id": "bafyPROG", "name": "Program", "xKey": "program"}]},
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
