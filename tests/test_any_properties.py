"""ADR-027 §4 — property handles, descriptor-aware writes, hydrated
reads and the definition surface of programs/any@v1."""

import json

import pytest
from test_any_module import client, load, wire

# A type in the descriptor vocabulary (any docs/27-descriptors.md): two
# single choices, a multiple choice, a multiple relation, a date, a
# number and a local-scope note. `Estimate` carries no xKey (the name
# is its handle); the others resolve by xKey.
_PROPS = {"properties": [
    {"id": "pSTAT", "name": "Status", "xKey": "status", "kind": "array",
     "scope": "synced",
     "xFormat": {"type": "choice", "pos": "a1", "options": {
         "todo": {"name": "To do", "color": "grey", "pos": "a0"},
         "done": {"name": "Done", "color": "green", "pos": "a1"}}}},
    {"id": "pPRIO", "name": "Priority", "xKey": "priority", "kind": "array",
     "xFormat": {"type": "choice", "pos": "a0", "options": {
         "high": {"name": "High", "color": "red", "pos": "a0"}}}},
    {"id": "pTAGS", "name": "Labels", "xKey": "labels", "kind": "array",
     "xFormat": {"type": "choice", "config": {"multiple": True}, "options": {
         "backend": {"name": "Backend", "color": "blue", "pos": "a0"}}}},
    {"id": "pREL", "name": "Related", "xKey": "related", "kind": "array",
     "xFormat": {"type": "relation", "config": {"multiple": True},
                 "relation": {"targetTypes": []}}},
    {"id": "pDUE", "name": "Due", "xKey": "due", "kind": "datetime",
     "xFormat": {"type": "date"}},
    {"id": "pEST", "name": "Estimate", "kind": "number"},
    {"id": "pNOTE", "name": "Note", "xKey": "note", "kind": "string",
     "scope": "local"},
]}
_TYPES = {"types": [{"id": "bafyTASK", "name": "Task", "xKey": "task"},
                    {"id": "bafyBOOK", "name": "Book", "xKey": "book"},
                    {"id": "page", "name": "page", "xKey": "page",
                     "hidden": True, "builtIn": True}]}
_ANY = {"properties": [
    {"id": "name", "name": "Name", "kind": "string", "scope": "synced"},
    {"id": "types", "kind": "array", "scope": "synced"}]}

DUNE = "bafyreiduneduneduneduneduneduneduneduneduneduneduneobj1"
HEAT = "bafyreiheatheatheatheatheatheatheatheatheatheatheatobj2"
OBJS = {DUNE: {"id": DUNE, "any": {"name": "Dune", "types": ["bafyBOOK", "page"]}},
        HEAT: {"id": HEAT, "any": {"name": "Heat", "types": ["bafyBOOK", "page"]}}}


def rig(records=(), by_name=None, extra=None):
    """A fake server that answers the catalog, an objects query (main
    result = `records`; `{"any.name": X}` lookups from `by_name`; `$in`
    lookups from OBJS) and every write with a stub id."""
    by_name = by_name or {"Dune": [OBJS[DUNE]], "Heat": [OBJS[HEAT]]}
    calls = []

    def fx(name, payload):
        if name == "config.get":
            return {"value": "http://any"}
        path = payload["url"].removeprefix("http://any").partition("?")[0]
        body = payload.get("json")
        verb = name.removeprefix("http.").upper()
        calls.append((verb, path, body))
        reply = {}
        if path.endswith("/types"):
            reply = _TYPES
        elif path.endswith("/types/bafyTASK/properties"):
            reply = _PROPS if verb == "GET" else {"propId": "pNEW"}
        elif path.endswith("/types/any/properties"):
            reply = _ANY
        elif path.endswith("/objects/query"):
            f = (body or {}).get("filter") or {}
            if "any.name" in f or "$and" in f:
                nm = f.get("any.name") or next(
                    (c["any.name"] for c in f.get("$and", []) if "any.name" in c), None)
                reply = {"records": by_name.get(nm, [])}
            elif isinstance(f.get("id"), dict) and "$in" in f["id"]:
                reply = {"records": [OBJS[i] for i in f["id"]["$in"] if i in OBJS]}
            else:
                reply = {"records": list(records)}
        elif path.endswith("/objects"):
            reply = {"objectId": "oNEW"}
        if extra:
            reply = extra(verb, path, body, reply)
        return {"status": 200, "headers": {}, "body": json.dumps(reply)}

    fx.calls = calls
    return fx


def posts(fx, suffix):
    return [(v, p, b) for v, p, b in fx.calls if p.endswith(suffix) and v != "GET"]


# --- handles ---------------------------------------------------------------

def test_xkey_is_the_handle_else_the_name():
    fx = rig()
    rows = client(fx).list_properties("s1", "task")
    handles = {r["id"]: r["handle"] for r in rows}
    assert handles["pSTAT"] == "status" and handles["pPRIO"] == "priority"
    assert handles["pTAGS"] == "labels" and handles["pREL"] == "related"
    assert handles["pEST"] == "Estimate" and handles["pNOTE"] == "note"
    # display order is xFormat.pos, positioned first
    assert [r["handle"] for r in rows][:2] == ["priority", "status"]
    assert rows[1]["options"] == [{"key": "todo", "name": "To do", "color": "grey"},
                                  {"key": "done", "name": "Done", "color": "green"}]
    # the name still resolves a property written by its label
    client(fx).update_object("s1", "o1", {"task": {"Status": "done"}})
    assert posts(fx, "/set/bafyTASK")[0][2] == {"patch": {"pSTAT": ["done"]}}


def test_reads_label_by_handle_and_show_option_names():
    fx = rig(records=[{"id": "o1", "any": {"name": "Ship", "types": ["bafyTASK"]},
                       "bafyTASK": {"pSTAT": ["done"], "pPRIO": ["high"],
                                    "pTAGS": ["backend", "ghost"], "pEST": 3}}])
    [rec] = client(fx).query_objects("s1", filter={"any.types": "task"})
    # a choice value is always an array of keys on the wire; names on read
    assert rec["task"] == {"status": ["Done"], "priority": ["High"],
                           "labels": ["Backend", "ghost"],   # dangling key raw
                           "Estimate": 3}


# --- writes ----------------------------------------------------------------

def test_choice_by_name_or_key_multiple_dedup_single_refuses_many():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {
        "status": "done", "priority": "high", "labels": ["Backend", "backend"]}})
    [(_, _, body)] = posts(fx, "/set/bafyTASK")
    assert body == {"patch": {"pSTAT": ["done"], "pPRIO": ["high"],
                              "pTAGS": ["backend"]}}
    assert r == {"objectId": "o1",
                 "resolved": {"task.status": ["done"], "task.priority": ["high"],
                              "task.labels": ["backend"]}}
    assert not posts(fx, "/properties/pTAGS")   # nothing minted
    with pytest.raises(ValueError, match='"status" is a single choice'):
        client(rig()).update_object("s1", "o1", {"task": {"status": ["done", "todo"]}})


def test_unknown_option_is_minted_before_the_value_write():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {"status": "In review"}})
    [(v, p, patch)] = posts(fx, "/types/bafyTASK/properties/pSTAT")
    assert v == "PATCH"
    # leaf-only sets under xFormat.options (ADR-027 §4)
    assert patch == {"set": {"xFormat.options.in_review.name": "In review",
                             "xFormat.options.in_review.color":
                                 patch["set"]["xFormat.options.in_review.color"],
                             "xFormat.options.in_review.pos": "a2"}}
    # PATCH lands before the set; value = the minted key
    order = [(v, p.rsplit("/", 1)[-1]) for v, p, _ in fx.calls if v != "GET"]
    assert order == [("PATCH", "pSTAT"), ("POST", "bafyTASK")]
    assert posts(fx, "/set/bafyTASK")[0][2] == {"patch": {"pSTAT": ["in_review"]}}
    assert r["createdOptions"] == [{"property": "status", "key": "in_review",
                                    "name": "In review",
                                    "color": patch["set"]["xFormat.options.in_review.color"]}]
    assert r["resolved"] == {"task.status": ["in_review"]}


def test_create_options_false_refuses_and_lists_options():
    fx = rig()
    with pytest.raises(ValueError, match='not an option of "status".*"To do", "Done"'):
        client(fx).update_object("s1", "o1", {"task": {"status": "Nope"}},
                                 create_options=False)
    assert [v for v, _, _ in fx.calls if v != "GET"] == []


def test_option_key_minting_uniquifies_and_casefolds():
    fx = rig()
    c = client(fx)
    c.update_object("s1", "o1", {"task": {"priority": "HIGH"}})   # casefold → high
    assert posts(fx, "/set/bafyTASK")[-1][2] == {"patch": {"pPRIO": ["high"]}}
    # a new name whose slug collides with an existing key gets _2
    r = c.update_object("s1", "o1", {"task": {"priority": "High!"}})
    assert r["createdOptions"][0]["key"] == "high_2"


def test_relations_accept_ids_uris_and_names():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {"related": [
        DUNE, "any://o/spaceX/" + HEAT, "Dune", {"id": HEAT}]}})
    assert posts(fx, "/set/bafyTASK")[0][2] == {
        "patch": {"pREL": ["any://" + DUNE, "any://" + HEAT]}}
    assert r["resolved"] == {"task.related": ["any://" + DUNE, "any://" + HEAT]}
    # the name lookup went to the objects query, exact any.name; the
    # explicit ids were verified in-space with one batched $in
    lookups = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert lookups == [{"filter": {"any.name": "Dune"}, "limit": 5},
                       {"filter": {"id": {"$in": [DUNE, HEAT]}}, "limit": 200}]


def test_relation_explicit_id_must_exist_in_the_space():
    # an id from another space is unresolvable by every reader (bare
    # any://<id>, no space segment) — refused before any write, with
    # the typed-link advice for cross-space references
    fx = rig()
    ghost = "bafyreighostghostghostghostghostghostghostghostghostobj9"
    with pytest.raises(ValueError, match="not in this space.*any://o/<spaceId>"):
        client(fx).update_object("s1", "o1", {"task": {"related": [DUNE, ghost]}})
    assert posts(fx, "/set/bafyTASK") == []


def test_relation_name_zero_or_many_matches_error_never_mint():
    fx = rig(by_name={"Twin": [OBJS[DUNE], OBJS[HEAT]]})
    c = client(fx)
    with pytest.raises(ValueError, match='no object named "Ghost".*relations never mint'):
        c.update_object("s1", "o1", {"task": {"related": "Ghost"}})
    with pytest.raises(ValueError, match='"Twin" names 2 objects.*book'):
        c.update_object("s1", "o1", {"task": {"related": "Twin"}})
    assert posts(fx, "/objects") == [] and posts(fx, "/set/bafyTASK") == []


def test_relation_honours_target_types_and_the_candidate_filter():
    # relation.targetTypes are type xKeys; relation.filter is one
    # JSON-text query condition — both narrow a name lookup
    props = json.loads(json.dumps(_PROPS))
    props["properties"][3]["xFormat"]["relation"] = {
        "targetTypes": ["book"], "filter": json.dumps({"any.name": {"$ne": ""}})}
    fx = rig(extra=lambda v, p, b, r: props
             if p.endswith("bafyTASK/properties") and v == "GET" else r)
    client(fx).update_object("s1", "o1", {"task": {"related": "Heat"}})
    lookups = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert lookups[0]["filter"] == {"$and": [{"any.name": "Heat"},
                                             {"any.name": {"$ne": ""}},
                                             {"any.types": {"$in": ["bafyBOOK"]}}]}


def test_single_relation_refuses_many():
    props = json.loads(json.dumps(_PROPS))
    props["properties"][3]["xFormat"].pop("config")
    fx = rig(extra=lambda v, p, b, r: props
             if p.endswith("bafyTASK/properties") and v == "GET" else r)
    with pytest.raises(ValueError, match='"related" is a single relation'):
        client(fx).update_object("s1", "o1", {"task": {"related": [DUNE, HEAT]}})


def test_dates_land_as_midnight_instants_and_kinds_are_checked():
    fx = rig()
    c = client(fx)
    c.update_object("s1", "o1", {"task": {"due": "2026-08-05T17:30:00Z",
                                          "Estimate": "4"}})
    body = posts(fx, "/set/bafyTASK")[0][2]["patch"]
    assert body["pDUE"] == {"$date": 1785888000000}   # 2026-08-05T00:00:00Z
    assert body["pEST"] == 4
    with pytest.raises(ValueError, match='"Estimate" is kind number: expected a number'):
        c.update_object("s1", "o1", {"task": {"Estimate": "four"}})
    with pytest.raises(ValueError, match='"due" is a date'):
        c.update_object("s1", "o1", {"task": {"due": "yesterday-ish"}})


def test_none_clears_via_unset_and_scopes_split_patches():
    fx = rig()
    client(fx).update_object("s1", "o1", {"task": {
        "Estimate": None, "status": "done", "note": "mine"}})
    unset = [b for v, p, b in fx.calls if p.endswith("/modify")]
    assert unset == [{"objectId": "o1", "dataset": "objects", "records": [
        {"id": "o1", "upsert": False,
         "ops": [{"type": "$unset", "path": "bafyTASK.pEST"}]}]}]
    # synced and local props go in separate set calls
    sets = [b["patch"] for _, _, b in posts(fx, "/set/bafyTASK")]
    assert sets == [{"pSTAT": ["done"]}, {"pNOTE": "mine"}]
    with pytest.raises(ValueError, match='"note" is scope local'):
        client(rig()).update_object("s1", "o1", {"task": {"note": None}})


def test_create_object_encodes_and_mints_before_the_post():
    fx = rig()
    r = client(fx).create_object("s1", {"types": ["task"], "name": "Ship", "initialProperties": {
        "task": {"status": "Blocked", "related": "Heat", "Estimate": None}}})
    order = [(v, p.rsplit("/", 1)[-1]) for v, p, _ in fx.calls if v not in ("GET",)
             and not p.endswith("/objects/query")]
    assert order == [("PATCH", "pSTAT"), ("POST", "objects")]
    body = posts(fx, "/objects")[0][2]
    assert body["initialProperties"] == {"any": {"name": "Ship"},
                                         "bafyTASK": {"pSTAT": ["blocked"],
                                                      "pREL": ["any://" + HEAT]}}
    assert r["objectId"] == "oNEW" and r["createdOptions"][0]["key"] == "blocked"


# --- hydration + filters -----------------------------------------------------

def test_links_hydrate_to_stubs_with_one_batched_query():
    fx = rig(records=[
        {"id": "o1", "any": {"types": ["bafyTASK"]},
         "bafyTASK": {"pREL": ["any://" + DUNE, "any://" + HEAT]}},
        {"id": "o2", "any": {"types": ["bafyTASK"]},
         "bafyTASK": {"pREL": ["any://" + DUNE, "any://bafyreighost"]}}])
    recs = client(fx).query_objects("s1", filter={"any.types": "task"})
    assert recs[0]["task"]["related"] == [
        {"id": DUNE, "name": "Dune", "types": ["book", "page"]},
        {"id": HEAT, "name": "Heat", "types": ["book", "page"]}]
    assert recs[1]["task"]["related"][1] == {"id": "bafyreighost", "name": None, "types": []}
    qs = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert len(qs) == 2 and qs[1]["filter"] == {"id": {"$in": [DUNE, HEAT, "bafyreighost"]}}


def test_normalize_false_keeps_raw_keys_and_values():
    fx = rig(records=[{"id": "o1", "bafyTASK": {"pSTAT": ["done"],
                                                "pREL": ["any://" + DUNE]}}])
    [rec] = client(fx).query_objects("s1", normalize=False)
    assert rec["bafyTASK"] == {"pSTAT": ["done"], "pREL": ["any://" + DUNE]}
    assert len([1 for v, p, b in fx.calls if p.endswith("/objects/query")]) == 1


def test_filters_accept_option_names_and_object_names():
    fx = rig()
    client(fx).query_objects("s1", filter={
        "any.types": "task", "task.status": "Done",
        "task.labels": {"$all": ["Backend"]}, "task.related": "Dune",
        "$or": [{"task.priority": {"$in": ["High", "high"]}}]})
    q = [b for v, p, b in fx.calls if p.endswith("/objects/query")][-1]
    assert q["filter"] == {"any.types": "bafyTASK", "bafyTASK.pSTAT": "done",
                           "bafyTASK.pTAGS": {"$all": ["backend"]},
                           "bafyTASK.pREL": "any://" + DUNE,
                           "$or": [{"bafyTASK.pPRIO": {"$in": ["high", "high"]}}]}


def test_filter_never_mints_an_option():
    with pytest.raises(ValueError, match="not an option"):
        client(rig()).query_objects("s1", filter={"task.status": "Nope"})


# --- definition surface ----------------------------------------------------

def test_set_option_creates_renames_recolors():
    fx = rig()
    c = client(fx)
    r = c.set_option("s1", "task", "status", "Blocked", color="red")
    assert r == {"key": "blocked", "name": "Blocked", "color": "red", "created": True}
    [(_, _, b)] = posts(fx, "/properties/pSTAT")
    assert b == {"set": {"xFormat.options.blocked.name": "Blocked",
                         "xFormat.options.blocked.color": "red",
                         "xFormat.options.blocked.pos": "a2"}}
    r = c.set_option("s1", "task", "status", "Done", name="Finished")
    assert r["created"] is False and r["key"] == "done"
    assert posts(fx, "/properties/pSTAT")[-1][2] == {
        "set": {"xFormat.options.done.name": "Finished"}}
    with pytest.raises(ValueError, match="color must be one of"):
        c.set_option("s1", "task", "status", "Done", color="mauve")
    with pytest.raises(ValueError, match="not a choice"):
        c.set_option("s1", "task", "Estimate", "x")


def test_remove_option_by_name_unsets_the_key():
    fx = rig()
    assert client(fx).remove_option("s1", "task", "labels", "Backend") == {"key": "backend"}
    assert posts(fx, "/properties/pTAGS")[0][2] == {"unset": ["xFormat.options.backend"]}


def test_patch_property_refuses_pinned_paths_and_keeps_typed_leaves():
    fx = rig()
    c = client(fx)
    for path in ("kind", "scope", "items", "properties"):
        with pytest.raises(ValueError, match="pinned"):
            c.patch_property("s1", "task", "status", set={path: "x"})
    # a set targets a leaf, never a container
    with pytest.raises(ValueError, match="set targets a leaf"):
        c.patch_property("s1", "task", "status", set={"xFormat.options": {}})
    # the slug moves within the kind; xFormat leaves keep their type,
    # everything else is a string
    c.patch_property("s1", "task", "status",
                     set={"xFormat.icon": "flag", "xFormat.pos": "b0",
                          "xFormat.config.multiple": True, "meta.index": "none",
                          "name": "State"},
                     unset=["xFormat.options.todo"])
    [(v, _, b)] = posts(fx, "/properties/pSTAT")
    assert v == "PATCH" and b == {
        "set": {"xFormat.icon": "flag", "xFormat.pos": "b0",
                "xFormat.config.multiple": True, "meta.index": "none", "name": "State"},
        "unset": ["xFormat.options.todo"]}


def test_delete_attach_detach_wire_shapes():
    fx = rig()
    c = client(fx)
    c.delete_property("s1", "task", "Estimate")
    c.attach_type("s1", "o1", "task")
    c.detach_type("s1", "o1", "task")
    assert [(v, p.split("/s1/")[1], b) for v, p, b in fx.calls if v != "GET"] == [
        ("DELETE", "types/bafyTASK/properties/pEST", None),
        ("POST", "properties/o1/attach/bafyTASK", None),
        ("POST", "properties/o1/detach/bafyTASK", None)]
    # the archived-property marker is gone with any-ui's meta bag
    g = load(wire())
    assert "archive_property" not in g


def test_reorder_property_re_expresses_positions():
    fx = rig()
    r = client(fx).reorder_property("s1", "task", "Estimate", after="")
    assert r["order"][0] == "Estimate"
    writes = [(p.rsplit("/", 1)[-1], b["set"]["xFormat.pos"])
              for v, p, b in fx.calls if v == "PATCH"]
    assert writes[0] == ("pEST", "a0")          # moved first
    positions = [w[1] for w in writes]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)


def test_add_property_seeds_options_and_validates_the_descriptor():
    fx = rig()
    c = client(fx)
    c.add_property("s1", "task", {"name": "Size", "xFormat": {
        "type": "choice", "options": {"s": "Small", "m": {"name": "Medium", "color": "blue"}}},
        "scope": "synced", "description": "T-shirt size"})
    body = posts(fx, "/types/bafyTASK/properties")[0][2]
    assert body["kind"] == "array"
    assert body["xFormat"]["options"] == {
        "s": {"name": "Small", "color": body["xFormat"]["options"]["s"]["color"], "pos": "a0"},
        "m": {"name": "Medium", "color": "blue", "pos": "a1"}}
    assert body["scope"] == "synced" and body["description"] == "T-shirt size"
    assert body["xFormat"]["pos"] == "a2"   # after the type's last pos (a1)
    assert "meta" not in body
    # a relation filter given as a condition object rides as JSON text
    c.add_property("s1", "task", {"name": "Owner", "xFormat": {
        "type": "relation", "relation": {"targetTypes": ["book"],
                                         "filter": {"any.name": {"$ne": ""}}}}})
    body = posts(fx, "/types/bafyTASK/properties")[-1][2]
    assert body["xFormat"]["relation"] == {"targetTypes": ["book"],
                                           "filter": json.dumps({"any.name": {"$ne": ""}})}
    with pytest.raises(ValueError, match="kind must be one of"):
        c.add_property("s1", "task", {"name": "X", "kind": "date"})


def test_flat_surface_exposes_the_new_tools_with_docs():
    g = load(wire())
    for name in ("patch_property", "set_option", "remove_option", "reorder_property",
                 "delete_property", "attach_type", "detach_type", "list_apps",
                 "list_available_apps", "setup_app", "links", "move_object"):
        assert callable(g[name]) and g[name].__doc__, name
    assert "createdOptions" in g["create_object"].__doc__
