"""programs/any@v1 — the guest space-data client, tested host-side by
exec-ing the module source with a fake `effect` global answering
http.* (json wire replies) and config.get."""

import json
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "programs" / "any@v1"
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
    fx = wire(replies={"/types": {"types": [], "typeId": "t2"}})
    c = client(fx)
    c.create_object("s1", {"typeId": "t"})
    c.create_type("s1", {"name": "T"})
    c.add_property("s1", "t1", {"name": "P"})
    assert [(v, p) for v, p, _ in fx.calls] == [
        ("POST", "/v1/spaces/s1/objects"),
        ("GET", "/v1/spaces/s1/types"),          # idempotency probe
        ("POST", "/v1/spaces/s1/types"),
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
    fx = wire(replies={"/types/t1/properties": {"propId": "p1"}})
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
    # user group + its props rekeyed to xKeys; builtins (any/nav/id) verbatim
    assert rec == {"id": "o1", "any": {"name": "Ship", "types": ["bafyTASK"]},
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
        ("GET", "/v1/spaces/s1/types/t1/properties")]


# --- markdown ---------------------------------------------------------------------

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
        {"id": "bafyTOOL", "name": "Any Tool", "xKey": "any_tool"}]},
    "/objects/query": {"records": [
        {"id": "p1", "any": {"name": "webSearch", "types": ["bafyPROG"]},
         "bafyPROG": {"bafyNAME": "webSearch", "bafyVER": "v1",
                      "bafyTOOL": True}},
        {"id": "p2", "any": {"name": "helper", "types": ["bafyPROG"]},
         "bafyPROG": {"bafyNAME": "helper", "bafyVER": "v2",
                      "bafyTOOL": False}}]},
    "/query": {"records": [{"id": "main", "text": "Does a thing.  "}]},
}


def test_list_programs_lists_a_space_sorted():
    fx = wire(replies=dict(_PROG_REPLIES))
    rows = client(fx).list_programs("repo1")
    assert rows == [
        {"name": "helper", "version": "v2", "anyTool": False,
         "description": "Does a thing."},
        {"name": "webSearch", "version": "v1", "anyTool": True,
         "description": "Does a thing."}]
    # the object query targeted the requested space with the program filter
    body = next(b for v, p, b in fx.calls if p.endswith("/objects/query"))
    assert body["filter"] == {"any.types": "bafyPROG"}


def test_list_programs_tools_only_filters():
    fx = wire(replies=dict(_PROG_REPLIES))
    rows = client(fx).list_programs("repo1", tools_only=True)
    assert [r["name"] for r in rows] == ["webSearch"]
