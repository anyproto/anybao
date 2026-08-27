"""ADR-022 — property handles, definition-aware writes, hydrated reads
and the definition surface of programs/any@v1."""

import json

import pytest
from test_any_module import client, load, wire

# A UI-made type: two Selects both stamped xKey "select" (any-ui's kind
# marker), a Multiselect with xKey "tags", a Relation with xKey "links",
# a format-bearing date, a Number with NO xKey, and one real slug xKey.
_PROPS = {"properties": [
    {"id": "pSTAT", "name": "Status", "xKey": "select", "kind": "string",
     "scope": "synced", "meta": {"pos": "a1"},
     "format": {"type": "select", "ui": "select", "options": {
         "todo": {"name": "To do", "color": "grey", "pos": "a0"},
         "done": {"name": "Done", "color": "green", "pos": "a1"}}}},
    {"id": "pPRIO", "name": "Priority", "xKey": "select", "kind": "string",
     "meta": {"pos": "a0"},
     "format": {"type": "select", "options": {
         "high": {"name": "High", "color": "red", "pos": "a0"}}}},
    {"id": "pTAGS", "name": "Labels", "xKey": "tags", "kind": "array",
     "format": {"type": "multiselect", "options": {
         "backend": {"name": "Backend", "color": "blue", "pos": "a0"}}}},
    {"id": "pREL", "name": "Related", "xKey": "links", "kind": "array",
     "format": {"type": "links", "ui": "links"}},
    {"id": "pDUE", "name": "Due", "kind": "datetime",
     "format": {"type": "date"}},
    {"id": "pEST", "name": "Estimate", "kind": "number"},
    {"id": "pNOTE", "name": "Note", "xKey": "note", "kind": "string",
     "scope": "local"},
    {"id": "pOLD", "name": "Old", "xKey": "old", "kind": "string",
     "meta": {"anyUiArchived": "1"}},
]}
_TYPES = {"types": [{"id": "bafyTASK", "name": "Task", "xKey": "task"},
                    {"id": "bafyPAGE", "name": "Page", "xKey": "page"},
                    {"id": "nav", "name": "Nav", "xKey": "nav"}]}
_ANY = {"properties": [
    {"id": "name", "name": "Name", "kind": "string", "scope": "synced"},
    {"id": "types", "kind": "array", "scope": "synced"}]}

DUNE = "bafyreiduneduneduneduneduneduneduneduneduneduneduneobj1"
HEAT = "bafyreiheatheatheatheatheatheatheatheatheatheatheatobj2"
OBJS = {DUNE: {"id": DUNE, "any": {"name": "Dune", "types": ["bafyPAGE"]}},
        HEAT: {"id": HEAT, "any": {"name": "Heat", "types": ["bafyPAGE"]}}}


def rig(records=(), by_name=None, extra=None):
    """A fake server that answers the catalog, an objects query (main
    result = `records`; `{"any.name": X}` lookups from `by_name`; `$in`
    lookups from OBJS) and every write with a stub id."""
    by_name = by_name or {"Dune": [OBJS[DUNE]], "Heat": [OBJS[HEAT]]}
    calls = []

    def fx(name, payload):
        if name == "config.get":
            return {"value": "http://any"}
        path = payload["url"].removeprefix("http://any")
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


# --- §1 handles ---------------------------------------------------------------

def test_marker_xkeys_resolve_by_name_and_never_collide():
    fx = rig()
    rows = client(fx).list_properties("s1", "task")
    handles = {r["id"]: r["handle"] for r in rows}
    # both Selects carry xKey "select" → handle = name; the slug xKey stays
    assert handles["pSTAT"] == "Status" and handles["pPRIO"] == "Priority"
    assert handles["pTAGS"] == "Labels" and handles["pREL"] == "Related"
    assert handles["pEST"] == "Estimate" and handles["pNOTE"] == "note"
    assert "pOLD" not in handles          # archived hidden by default
    assert [r["handle"] for r in rows][:2] == ["Priority", "Status"]  # meta.pos
    assert rows[1]["options"] == [{"key": "todo", "name": "To do", "color": "grey"},
                                  {"key": "done", "name": "Done", "color": "green"}]
    assert any(r["id"] == "pOLD" and r["archived"]
               for r in client(fx).list_properties("s1", "task", include_archived=True))


def test_ambiguous_marker_key_errors_listing_candidates():
    fx = rig()
    with pytest.raises(ValueError,
                       match='"select" is ambiguous.*(Status.*Priority|Priority.*Status)'):
        client(fx).update_object("s1", "o1", {"task": {"select": "done"}})
    assert posts(fx, "/set/bafyTASK") == []


def test_reads_label_by_handle_and_show_option_names():
    fx = rig(records=[{"id": "o1", "any": {"name": "Ship", "types": ["bafyTASK"]},
                       "bafyTASK": {"pSTAT": "done", "pPRIO": "high",
                                    "pTAGS": ["backend", "ghost"], "pEST": 3}}])
    [rec] = client(fx).query_objects("s1", filter={"any.types": "task"})
    assert rec["task"] == {"Status": "Done", "Priority": "High",
                           "Labels": ["Backend", "ghost"],   # dangling key raw
                           "Estimate": 3}


# --- §2 writes ----------------------------------------------------------------

def test_select_by_name_or_key_multiselect_dedup():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {
        "Status": "done", "Priority": "high", "Labels": ["Backend", "backend"]}})
    [(_, _, body)] = posts(fx, "/set/bafyTASK")
    assert body == {"patch": {"pSTAT": "done", "pPRIO": "high",
                              "pTAGS": ["backend"]}}
    assert r == {"objectId": "o1",
                 "resolved": {"task.Labels": ["backend"]}}
    assert not posts(fx, "/properties/pTAGS")   # nothing minted


def test_unknown_option_is_minted_before_the_value_write():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {"Status": "In review"}})
    [(v, p, patch)] = posts(fx, "/types/bafyTASK/properties/pSTAT")
    assert v == "PATCH"
    assert patch == {"set": {"format.options.in_review.name": "In review",
                             "format.options.in_review.color":
                                 patch["set"]["format.options.in_review.color"],
                             "format.options.in_review.pos": "a2"}}
    # PATCH lands before the set; value = the minted key
    order = [(v, p.rsplit("/", 1)[-1]) for v, p, _ in fx.calls if v != "GET"]
    assert order == [("PATCH", "pSTAT"), ("POST", "bafyTASK")]
    assert posts(fx, "/set/bafyTASK")[0][2] == {"patch": {"pSTAT": "in_review"}}
    assert r["createdOptions"] == [{"property": "Status", "key": "in_review",
                                    "name": "In review",
                                    "color": patch["set"]["format.options.in_review.color"]}]
    assert r["resolved"] == {"task.Status": "in_review"}


def test_create_options_false_refuses_and_lists_options():
    fx = rig()
    with pytest.raises(ValueError, match='not an option of "Status".*"To do", "Done"'):
        client(fx).update_object("s1", "o1", {"task": {"Status": "Nope"}},
                                 create_options=False)
    assert [v for v, _, _ in fx.calls if v != "GET"] == []


def test_option_key_minting_uniquifies_and_casefolds():
    fx = rig()
    c = client(fx)
    c.update_object("s1", "o1", {"task": {"Priority": "HIGH"}})   # casefold → high
    assert posts(fx, "/set/bafyTASK")[-1][2] == {"patch": {"pPRIO": "high"}}
    # a new name whose slug collides with an existing key gets _2
    r = c.update_object("s1", "o1", {"task": {"Priority": "High!"}})
    assert r["createdOptions"][0]["key"] == "high_2"


def test_links_accept_ids_uris_and_names():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {"Related": [
        DUNE, "any://o/spaceX/" + HEAT, "Dune", {"id": HEAT}]}})
    assert posts(fx, "/set/bafyTASK")[0][2] == {
        "patch": {"pREL": ["any://" + DUNE, "any://" + HEAT]}}
    assert r["resolved"] == {"task.Related": ["any://" + DUNE, "any://" + HEAT]}
    # the name lookup went to the objects query, exact any.name
    lookups = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert lookups == [{"filter": {"any.name": "Dune"}, "limit": 5}]


def test_links_name_zero_or_many_matches_error_never_mint():
    fx = rig(by_name={"Twin": [OBJS[DUNE], OBJS[HEAT]]})
    c = client(fx)
    with pytest.raises(ValueError, match='no object named "Ghost".*links never mint'):
        c.update_object("s1", "o1", {"task": {"Related": "Ghost"}})
    with pytest.raises(ValueError, match='"Twin" names 2 objects.*page'):
        c.update_object("s1", "o1", {"task": {"Related": "Twin"}})
    assert posts(fx, "/objects") == [] and posts(fx, "/set/bafyTASK") == []


def test_links_honour_the_definitions_candidate_filter():
    props = json.loads(json.dumps(_PROPS))
    props["properties"][3]["format"]["filter"] = {"any.types": "bafyPAGE"}
    fx = rig(extra=lambda v, p, b, r: props
             if p.endswith("bafyTASK/properties") and v == "GET" else r)
    client(fx).update_object("s1", "o1", {"task": {"Related": "Heat"}})
    lookups = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert lookups[0]["filter"] == {"$and": [{"any.types": "bafyPAGE"},
                                             {"any.name": "Heat"}]}


def test_dates_land_as_midnight_instants_and_kinds_are_checked():
    fx = rig()
    c = client(fx)
    c.update_object("s1", "o1", {"task": {"Due": "2026-08-05T17:30:00Z",
                                          "Estimate": "4"}})
    body = posts(fx, "/set/bafyTASK")[0][2]["patch"]
    assert body["pDUE"] == {"$date": 1785888000000}   # 2026-08-05T00:00:00Z
    assert body["pEST"] == 4
    with pytest.raises(ValueError, match='"Estimate" is kind number: expected a number'):
        c.update_object("s1", "o1", {"task": {"Estimate": "four"}})
    with pytest.raises(ValueError, match='"Due" is a date'):
        c.update_object("s1", "o1", {"task": {"Due": "yesterday-ish"}})


def test_none_clears_via_unset_and_scopes_split_patches():
    fx = rig()
    client(fx).update_object("s1", "o1", {"task": {
        "Estimate": None, "Status": "done", "note": "mine"}})
    unset = [b for v, p, b in fx.calls if p.endswith("/modify")]
    assert unset == [{"objectId": "o1", "dataset": "objects", "records": [
        {"id": "o1", "upsert": False,
         "ops": [{"type": "$unset", "path": "bafyTASK.pEST"}]}]}]
    # synced and local props go in separate set calls
    sets = [b["patch"] for _, _, b in posts(fx, "/set/bafyTASK")]
    assert sets == [{"pSTAT": "done"}, {"pNOTE": "mine"}]
    with pytest.raises(ValueError, match='"note" is scope local'):
        client(rig()).update_object("s1", "o1", {"task": {"note": None}})


def test_create_object_encodes_and_mints_before_the_post():
    fx = rig()
    r = client(fx).create_object("s1", {"types": ["task"], "name": "Ship", "initialProperties": {
        "task": {"Status": "Blocked", "Related": "Heat", "Estimate": None}}})
    order = [(v, p.rsplit("/", 1)[-1]) for v, p, _ in fx.calls if v not in ("GET",)
             and not p.endswith("/objects/query")]
    assert order == [("PATCH", "pSTAT"), ("POST", "objects")]
    body = posts(fx, "/objects")[0][2]
    assert body["initialProperties"] == {"any": {"name": "Ship"},
                                         "bafyTASK": {"pSTAT": "blocked",
                                                      "pREL": ["any://" + HEAT]}}
    assert r["objectId"] == "oNEW" and r["createdOptions"][0]["key"] == "blocked"


def test_archived_property_write_warns():
    fx = rig()
    r = client(fx).update_object("s1", "o1", {"task": {"old": "x"}})
    assert "archived" in r["warnings"][0]


# --- §3 hydration + filters -----------------------------------------------------

def test_links_hydrate_to_stubs_with_one_batched_query():
    fx = rig(records=[
        {"id": "o1", "any": {"types": ["bafyTASK"]},
         "bafyTASK": {"pREL": ["any://" + DUNE, "any://" + HEAT]}},
        {"id": "o2", "any": {"types": ["bafyTASK"]},
         "bafyTASK": {"pREL": ["any://" + DUNE, "any://bafyreighost"]}}])
    recs = client(fx).query_objects("s1", filter={"any.types": "task"})
    assert recs[0]["task"]["Related"] == [
        {"id": DUNE, "name": "Dune", "types": ["page"]},
        {"id": HEAT, "name": "Heat", "types": ["page"]}]
    assert recs[1]["task"]["Related"][1] == {"id": "bafyreighost", "name": None, "types": []}
    qs = [b for v, p, b in fx.calls if p.endswith("/objects/query")]
    assert len(qs) == 2 and qs[1]["filter"] == {"id": {"$in": [DUNE, HEAT, "bafyreighost"]}}


def test_normalize_false_keeps_raw_keys_and_values():
    fx = rig(records=[{"id": "o1", "bafyTASK": {"pSTAT": "done",
                                                "pREL": ["any://" + DUNE]}}])
    [rec] = client(fx).query_objects("s1", normalize=False)
    assert rec["bafyTASK"] == {"pSTAT": "done", "pREL": ["any://" + DUNE]}
    assert len([1 for v, p, b in fx.calls if p.endswith("/objects/query")]) == 1


def test_filters_accept_option_names_and_object_names():
    fx = rig()
    client(fx).query_objects("s1", filter={
        "any.types": "task", "task.Status": "Done",
        "task.Labels": {"$all": ["Backend"]}, "task.Related": "Dune",
        "$or": [{"task.Priority": {"$in": ["High", "high"]}}]})
    q = [b for v, p, b in fx.calls if p.endswith("/objects/query")][-1]
    assert q["filter"] == {"any.types": "bafyTASK", "bafyTASK.pSTAT": "done",
                           "bafyTASK.pTAGS": {"$all": ["backend"]},
                           "bafyTASK.pREL": "any://" + DUNE,
                           "$or": [{"bafyTASK.pPRIO": {"$in": ["high", "high"]}}]}


def test_filter_never_mints_an_option():
    with pytest.raises(ValueError, match="not an option"):
        client(rig()).query_objects("s1", filter={"task.Status": "Nope"})


# --- §4 definition surface ----------------------------------------------------

def test_set_option_creates_renames_recolors():
    fx = rig()
    c = client(fx)
    r = c.set_option("s1", "task", "Status", "Blocked", color="red")
    assert r == {"key": "blocked", "name": "Blocked", "color": "red", "created": True}
    [(_, _, b)] = posts(fx, "/properties/pSTAT")
    assert b == {"set": {"format.options.blocked.name": "Blocked",
                         "format.options.blocked.color": "red",
                         "format.options.blocked.pos": "a2"}}
    r = c.set_option("s1", "task", "Status", "Done", name="Finished")
    assert r["created"] is False and r["key"] == "done"
    assert posts(fx, "/properties/pSTAT")[-1][2] == {
        "set": {"format.options.done.name": "Finished"}}
    with pytest.raises(ValueError, match="color must be one of"):
        c.set_option("s1", "task", "Status", "Done", color="mauve")
    with pytest.raises(ValueError, match="not a select"):
        c.set_option("s1", "task", "Estimate", "x")


def test_remove_option_by_name_unsets_the_key():
    fx = rig()
    assert client(fx).remove_option("s1", "task", "Labels", "Backend") == {"key": "backend"}
    assert posts(fx, "/properties/pTAGS")[0][2] == {"unset": ["format.options.backend"]}


def test_patch_property_refuses_pinned_paths_and_stringifies_leaves():
    fx = rig()
    c = client(fx)
    for path in ("kind", "format.type", "scope", "format"):
        with pytest.raises(ValueError, match="pinned"):
            c.patch_property("s1", "task", "Status", set={path: "x"})
    c.patch_property("s1", "task", "Status", set={"meta.icon": "flag", "meta.pos": "b0"},
                     unset=["format.options.todo"])
    [(v, _, b)] = posts(fx, "/properties/pSTAT")
    assert v == "PATCH" and b == {"set": {"meta.icon": "flag", "meta.pos": "b0"},
                                  "unset": ["format.options.todo"]}


def test_archive_delete_attach_detach_wire_shapes():
    fx = rig()
    c = client(fx)
    c.archive_property("s1", "task", "Estimate")
    c.archive_property("s1", "task", "Estimate", restore=True)
    c.delete_property("s1", "task", "Estimate")
    c.attach_type("s1", "o1", "task")
    c.detach_type("s1", "o1", "task")
    assert [(v, p.split("/s1/")[1], b) for v, p, b in fx.calls if v != "GET"] == [
        ("PATCH", "types/bafyTASK/properties/pEST", {"set": {"meta.anyUiArchived": "1"}}),
        ("PATCH", "types/bafyTASK/properties/pEST", {"unset": ["meta.anyUiArchived"]}),
        ("DELETE", "types/bafyTASK/properties/pEST", None),
        ("POST", "properties/o1/attach/bafyTASK", None),
        ("POST", "properties/o1/detach/bafyTASK", None)]


def test_reorder_property_re_expresses_positions():
    fx = rig()
    r = client(fx).reorder_property("s1", "task", "Estimate", after="")
    assert r["order"][0] == "Estimate"
    writes = [(p.rsplit("/", 1)[-1], b["set"]["meta.pos"])
              for v, p, b in fx.calls if v == "PATCH"]
    assert writes[0] == ("pEST", "a0")          # moved first
    positions = [w[1] for w in writes]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)


def test_add_property_seeds_options_and_validates_format():
    fx = rig()
    c = client(fx)
    c.add_property("s1", "task", {"name": "Size", "format": {
        "type": "select", "options": {"s": "Small", "m": {"name": "Medium", "color": "blue"}}},
        "scope": "synced", "description": "T-shirt size"})
    body = posts(fx, "/types/bafyTASK/properties")[0][2]
    assert body["format"]["options"] == {
        "s": {"name": "Small", "color": body["format"]["options"]["s"]["color"], "pos": "a0"},
        "m": {"name": "Medium", "color": "blue", "pos": "a1"}}
    assert body["scope"] == "synced" and body["description"] == "T-shirt size"
    assert body["meta"] == {"pos": "a2"}   # after the type's last pos (a1)
    with pytest.raises(ValueError, match="format.type must be one of"):
        c.add_property("s1", "task", {"name": "X", "format": {"type": "tags"}})
    with pytest.raises(ValueError, match="kind must be one of"):
        c.add_property("s1", "task", {"name": "X", "kind": "date"})


def test_flat_surface_exposes_the_new_tools_with_docs():
    g = load(wire())
    for name in ("patch_property", "set_option", "remove_option", "reorder_property",
                 "archive_property", "delete_property", "attach_type", "detach_type"):
        assert callable(g[name]) and g[name].__doc__, name
    assert "createdOptions" in g["create_object"].__doc__
