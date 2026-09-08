"""programs/recall@v1 — the guest recall primitives, tested host-side
by exec-ing both module sources with a fake `effect` global that
answers the any server's routes; `use("any@v1")` resolves to the
exec-ed any module namespace."""

import json
from pathlib import Path
from types import SimpleNamespace

from kernelenv import kernel_globals

_K = kernel_globals()


def at(seconds):
    return _K["instant"](seconds)

PROGRAMS = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
ANY_SRC = (PROGRAMS / "any@v1" / "program.py").read_text()
RECALL_SRC = (PROGRAMS / "recall@v1" / "program.py").read_text()

# a search hit names its collection; any@v1 adds the store `key`
HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
       "key": "agent_memory_items", "recordId": "m1", "score": 0.9}

# One object row with a user type t1: p_ref / p_refs are relation
# props (the object-ref descriptor — arrays of any:// URIs; p_ref
# exercises scalar tolerance), p_str a plain string; `any` and the
# hidden built-ins' groups are reserved and must be skipped.
OBJ_ROW = {
    "id": "obj1",
    "any": {"name": "Dune", "types": ["t1", "page"]},
    "page": {},
    "t1": {"p_ref": "any://target1", "p_refs": ["any://target2", "any://target3"],
           "p_str": "prose"},
}
T1_PROPS = [
    {"id": "p_ref", "name": "Author", "kind": "array", "xFormat": {"type": "relation"}},
    {"id": "p_refs", "name": "Mentions", "kind": "array",
     "xFormat": {"type": "relation", "config": {"multiple": True}}},
    {"id": "p_str", "name": "Notes", "kind": "string"},
]
# the store hosts and the types they carry (the key → collection
# resolution reads them; collections equal their keys in this rig)
HOST_TYPES = {"brain1": ["br"], "brainX": ["br"], "chat1": ["lg"], "chat9": ["lg"]}


def _reply(status, data):
    return {"status": status, "headers": {}, "body": json.dumps(data)}


def fake_any(capture, *, memory=(), turns=(), chunks=(), brain=None):
    """The server's routes over the http syscall, captured as
    (VERB, path, json-body) like the wire. `brain` answers the
    ADR-017 brain-child derive (None → 404 bundle.not_found, like a
    space where the harness never registered bao/v1)."""
    def fx(name, payload):
        assert name.startswith("http."), name
        path = payload["url"].removeprefix("http://any").partition("?")[0]
        body = payload.get("json")
        if path.endswith("/v1/spaces"):   # name-resolution plumbing (§8):
            return _reply(200, {"spaces": []})   # uncaptured, indices stable
        # ADR-017 / ADR-027 store plumbing — uncaptured so dataset-call
        # indices stay stable across tests
        if path.endswith("/bundles"):
            # every chat in the fixtures is a bundle root
            return _reply(200, {"bundles": [
                {"id": "system:general-chat/v1", "rootId": "chat9", "derived": True},
                {"id": "chat1-bundle/v1", "rootId": "chat1"}]})
        if "/types/" in path and path.endswith("/datasets"):
            def ds(key, **rest):
                return {"id": "d_" + key, "key": key, "collection": key,
                        "module": "records", **rest}
            return _reply(200, {"datasets": [
                ds("agent_memory_items",
                   search={"title": "context", "text": "body", "scope": "agent"}),
                ds("agent_job_state"), ds("agent_roi_injections"),
                ds("agent_turns",
                   search={"title": "userText", "text": "searchText",
                           "scope": "history"}),
                ds("agent_chunks", search={"text": "summary", "scope": "history"})]})
        if path.endswith("/objects/query") and isinstance((body or {}).get("filter"), dict) \
                and isinstance(body["filter"].get("id"), str) \
                and body["filter"]["id"] in HOST_TYPES:
            oid = body["filter"]["id"]   # a store host's carried types
            return _reply(200, {"records": [{"id": oid, "any": {"types": HOST_TYPES[oid]}}]})
        if path.endswith("/children"):
            if body and body.get("seed") == "bao/brain/v1":
                if brain is None:
                    return _reply(404, {"error": {
                        "code": "bundle.not_found", "message": "no bao/v1"}})
                capture.append(("POST", path, body))
                return _reply(200, {"objectId": brain})
            # a chat's log child hosts the same datasets under the
            # chat's own id in these fixtures (host identity is what
            # the assertions check)
            root = path.rsplit("/bundles/", 1)[1].removesuffix("/children")
            chat = {"system%3Ageneral-chat%2Fv1": "chat9",
                    "chat1-bundle%2Fv1": "chat1"}.get(root, "chat9")
            return _reply(200, {"objectId": chat})
        capture.append((name.removeprefix("http.").upper(), path, body))
        if path.endswith("/search"):
            return _reply(200, {"hits": [HIT], "mode": "hybrid", "vectorStatus": "used"})
        if path.endswith("/objects/query"):
            return _reply(200, {"records": [OBJ_ROW]})
        if path.endswith("/types/t1/properties"):
            return _reply(200, {"properties": T1_PROPS})
        if path.endswith("/types"):
            return _reply(200, {"types": [
                {"id": "t1", "name": "T One", "xKey": "t_one"},
                {"id": "br", "name": "Agent Brain", "xKey": "agent_brain"},
                {"id": "lg", "name": "Agent Log", "xKey": "agent_log"}]})
        if path.endswith("/backlinks"):
            return _reply(200, {"object": [
                {"source": {"spaceId": "s1", "objectId": "src9", "dataset": "prop",
                            "recordId": "p_ref", "typeId": "t1"},
                 "kind": "relation", "target": {"uri": "any://o/s1/obj1"}},
                {"source": {"spaceId": "s1", "objectId": "pg1", "dataset": "editor_blocks",
                            "recordId": "blk"},
                 "kind": "link", "target": {"uri": "any://o/s1/obj1"}}], "parts": []})
        if path.endswith("/query"):  # per-object dataset query
            recs = {"agent_memory_items": list(memory), "agent_turns": list(turns),
                    "agent_chunks": list(chunks)}[body["dataset"]]
            return _reply(200, {"records": recs})
        return _reply(404, {"error": {"code": "unknown", "message": path}})
    return fx


def build(fx, space="s1", **recall_kw):
    nospan = lambda name=None, kind=None: (lambda f: f)  # noqa: E731
    any_g = {"effect": fx, "span": nospan, "use": None, **kernel_globals()}
    exec(compile(ANY_SRC, "any@v1.py", "exec"), any_g)
    any_g["_instance"] = any_g["_Client"]("http://any")   # skip config.get
    any_mod = SimpleNamespace(**any_g)
    rec_g = {"effect": fx, "span": nospan, **kernel_globals(),
             "use": lambda spec: {"any@v1": any_mod}[spec]}
    exec(compile(RECALL_SRC, "recall@v1.py", "exec"), rec_g)
    return rec_g["recall"](any_mod, space, **recall_kw)


def recall(capture, **fake_kw):
    return build(fake_any(capture, **fake_kw),
                 brain_object_id="brain1", chat_object_id="chat1")


# --- search ------------------------------------------------------------------

def test_search_unwraps_hits_and_passes_scopes_limit():
    cap = []
    hits = recall(cap).search("qwery", scopes=("agent", "history"), limit=5)
    assert hits == [HIT]
    method, path, body = cap[0]
    assert (method, path) == ("POST", "/v1/spaces/s1/search")
    assert body == {"query": "qwery", "scopes": ["agent", "history"], "limit": 5}


def test_search_defaults_all_four_scopes():
    cap = []
    recall(cap).search("q")
    assert cap[0][2]["scopes"] == ["agent", "history", "basic", "email"]


def test_search_rejects_empty_query_before_the_wire():
    # the index has no browse-all mode; an empty query must fail
    # client-side with the enumeration hint, never reach the server
    import pytest
    cap = []
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="agent_memory_items"):
            recall(cap).search(bad)
    assert cap == []


# --- hydrate -----------------------------------------------------------------

def test_hydrate_pairs_hits_with_records_one_in_query():
    cap = []
    r = recall(cap, memory=[{"id": "m1", "content": "remembered"}])
    pairs = r.hydrate([HIT, {**HIT, "recordId": "missing"}])
    assert pairs == [(HIT, {"id": "m1", "content": "remembered"})]  # missing dropped
    queries = [(p, b) for _, p, b in [c for c in cap if c[1].endswith("/query")]]
    assert queries == [("/v1/spaces/s1/query", {
        "objectId": "brain1", "dataset": "agent_memory_items",
        "filter": {"id": {"$in": ["m1", "missing"]}}})]


# --- by_period ---------------------------------------------------------------

def test_by_period_fans_out_merges_and_time_sorts():
    cap = []
    r = recall(cap,
               memory=[{"id": "m1", "validFrom": at(300)}],
               turns=[{"id": "t1", "createdAt": at(100)},
                      {"id": "t2", "createdAt": {"$date": "1970-01-01T00:06:40Z"}}],
               chunks=[{"id": "c1", "periodStart": at(200), "periodEnd": at(350)}])
    recs = r.by_period(100, 400)
    assert [(x["id"], x["source"]) for x in recs] == [
        ("t1", "turn"), ("c1", "chunk"), ("m1", "memory"), ("t2", "turn")]


def test_by_period_wire_filters():
    cap = []
    recall(cap).by_period(100, 400)
    by_dataset = {b["dataset"]: b for _, _, b in cap
                  if b and "dataset" in b}
    assert by_dataset["agent_memory_items"]["objectId"] == "brain1"
    # ADR-019 §3: one instant literal shape on every source
    lo, hi = at(100), at(400)
    assert by_dataset["agent_memory_items"]["filter"] == {
        "validFrom": {"$gte": lo, "$lte": hi}}
    assert by_dataset["agent_turns"]["objectId"] == "chat1"
    assert by_dataset["agent_turns"]["filter"] == {"createdAt": {"$gte": lo, "$lte": hi}}
    # chunks match on period OVERLAP, not containment
    assert by_dataset["agent_chunks"]["filter"] == {
        "periodStart": {"$lte": hi}, "periodEnd": {"$gte": lo}}


def test_by_period_takes_iso_bounds_and_refuses_bare_literals_downstream():
    cap = []
    recall(cap).by_period("1970-01-01T00:01:40Z", "1970-01-01T00:06:40Z")
    turns = next(b for _, _, b in cap if b and b.get("dataset") == "agent_turns")
    assert turns["filter"] == {"createdAt": {"$gte": at(100), "$lte": at(400)}}


def test_by_period_skips_sources_without_object_id():
    cap = []
    r = build(fake_any(cap), brain_object_id="brain1")
    assert r.by_period(0, 1) == []
    assert [b["dataset"] for _, _, b in cap] == ["agent_memory_items"]  # no chat queries


def test_binder_derives_chat_id_from_space_config_mapping():
    cap = []
    r = build(fake_any(cap, brain="brainX"),
              space={"spaceId": "s1", "chatId": "chat9"})
    r.by_period(0, 1)
    by_dataset = {b["dataset"]: b for _, _, b in cap
                  if b and "dataset" in b}
    assert by_dataset["agent_turns"]["objectId"] == "chat9"


def test_by_period_resolves_brain_lazily_once():
    cap = []
    r = build(fake_any(cap, brain="brainX",
                       memory=[{"id": "m1", "validFrom": 0}]),
              chat_object_id="chat1")
    assert [x["id"] for x in r.by_period(0, 1)] == ["m1"]
    r.by_period(0, 1)
    brain_gets = [p for _, p, _ in cap if p.endswith("/children")]
    assert len(brain_gets) == 1  # child derive cached after the first resolve
    by_dataset = {b["dataset"]: b for _, _, b in cap
                  if b and "dataset" in b}
    assert by_dataset["agent_memory_items"]["objectId"] == "brainX"


def test_by_period_raises_when_no_source_binds():
    import pytest
    r = build(fake_any([]))  # bare string space, no brain in the space
    with pytest.raises(ValueError, match="no sources"):
        r.by_period(0, 1)
    # search/hydrate stay usable on the same bind
    assert r.search("q") == [HIT]


# --- neighbors ---------------------------------------------------------------

def test_neighbors_forward_refs_only_links_props():
    cap = []
    got = recall(cap).neighbors("obj1")
    # type/prop are xKeys (name fallback when the prop has no xKey) —
    # content ids never surface in either direction; a block edge
    # names its collection
    assert got["backlinks"] == [
        {"sourceId": "src9", "kind": "relation", "type": "t_one", "prop": "Author"},
        {"sourceId": "pg1", "kind": "link", "dataset": "editor_blocks"}]
    # any:// prefixes stripped — targets speak bare object ids
    assert sorted(f["targetId"] for f in got["forward"]) == ["target1", "target2", "target3"]
    by_target = {f["targetId"]: f for f in got["forward"]}
    assert by_target["target1"]["prop"] == "Author"
    assert by_target["target2"]["prop"] == "Mentions"
    # p_str (no relation slug) contributed nothing; reserved any/page skipped
    assert all(f["prop"] != "Notes" for f in got["forward"])
    assert all(f["type"] == "t_one" for f in got["forward"])


def test_neighbors_queries_row_by_id():
    cap = []
    recall(cap).neighbors("obj1")
    method, path, body = cap[0]
    assert path == "/v1/spaces/s1/objects/query"
    assert body["filter"] == {"id": "obj1"} and body["limit"] == 1


def test_neighbors_unknown_object_is_empty():
    def fx(name, payload):
        path = payload["url"]
        if path.endswith("/backlinks"):
            return _reply(200, {"object": [], "parts": []})
        return _reply(200, {"records": []})
    assert build(fx).neighbors("nope") == {"forward": [], "backlinks": []}


def test_neighbors_tolerates_unqueryable_type_group():
    def fx(name, payload):
        path = payload["url"]
        if path.endswith("/v1/spaces"):
            return _reply(200, {"spaces": []})   # name-resolution plumbing (§8)
        if path.endswith("/objects/query"):
            return _reply(200, {"records": [{"id": "o", "ghost": {"p": "x"}}]})
        if path.endswith("/backlinks"):
            return _reply(200, {"object": [], "parts": []})
        return _reply(404, {"error": {"code": "type.not_found", "message": "ghost"}})
    assert build(fx).neighbors("o") == {"forward": [], "backlinks": []}
