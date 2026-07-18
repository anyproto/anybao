"""programs/recall@v1 — the guest recall primitives, tested host-side
by exec-ing both module sources with a fake `effect` global that
answers the any server's routes; `use("any@v1")` resolves to the
exec-ed any module namespace."""

import json
from pathlib import Path
from types import SimpleNamespace

PROGRAMS = Path(__file__).resolve().parents[1] / "programs"
ANY_SRC = (PROGRAMS / "any@v1" / "program.py").read_text()
RECALL_SRC = (PROGRAMS / "recall@v1" / "program.py").read_text()

HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
       "recordId": "m1", "score": 0.9}

# One object row with a user type t1: p_ref / p_refs are links-format
# props (the object-ref convention — arrays of any:// URIs; p_ref
# exercises scalar tolerance), p_str a plain string; `any` and `nav`
# are reserved and must be skipped.
OBJ_ROW = {
    "id": "obj1",
    "any": {"name": "Dune"},
    "nav": {"parentId": "root"},
    "t1": {"p_ref": "any://target1", "p_refs": ["any://target2", "any://target3"],
           "p_str": "prose"},
}
T1_PROPS = [
    {"id": "p_ref", "name": "Author", "kind": "array", "format": {"type": "links"}},
    {"id": "p_refs", "name": "Mentions", "kind": "array",
     "format": {"type": "links", "ui": "links"}},
    {"id": "p_str", "name": "Notes", "kind": "string"},
]


def _reply(status, data):
    return {"status": status, "headers": {}, "body": json.dumps(data)}


def fake_any(capture, *, memory=(), turns=(), chunks=()):
    """The server's routes over the http syscall, captured as
    (VERB, path, json-body) like the wire."""
    def fx(name, payload):
        assert name.startswith("http."), name
        path = payload["url"].removeprefix("http://any")
        body = payload.get("json")
        capture.append((name.removeprefix("http.").upper(), path, body))
        if path.endswith("/search"):
            return _reply(200, {"hits": [HIT], "mode": "hybrid", "vectorStatus": "used"})
        if path.endswith("/objects/query"):
            return _reply(200, {"records": [OBJ_ROW]})
        if path.endswith("/types/t1/properties"):
            return _reply(200, {"properties": T1_PROPS})
        if path.endswith("/backlinks"):
            return _reply(200, {"backlinks": [
                {"objectId": "src9", "typeId": "t1", "propId": "p_ref"}]})
        if path.endswith("/query"):  # per-object dataset query
            recs = {"agent_memory_items": list(memory), "agent_turns": list(turns),
                    "agent_chunks": list(chunks)}[body["dataset"]]
            return _reply(200, {"records": recs})
        return _reply(404, {"error": {"code": "unknown", "message": path}})
    return fx


def build(fx, space="s1", **recall_kw):
    nospan = lambda name, kind=None: (lambda f: f)  # noqa: E731
    any_g = {"effect": fx, "span": nospan, "use": None}
    exec(compile(ANY_SRC, "any@v1.py", "exec"), any_g)
    any_mod = SimpleNamespace(**any_g)
    rec_g = {"effect": fx, "span": nospan,
             "use": lambda spec: {"any@v1": any_mod}[spec]}
    exec(compile(RECALL_SRC, "recall@v1.py", "exec"), rec_g)
    return rec_g["recall"](any_g["client"]("http://any"), space, **recall_kw)


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


def test_search_defaults_all_three_scopes():
    cap = []
    recall(cap).search("q")
    assert cap[0][2]["scopes"] == ["agent", "history", "basic"]


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
               memory=[{"id": "m1", "validFrom": 300}],
               turns=[{"id": "t1", "createdAt": 100}, {"id": "t2", "createdAt": 400}],
               chunks=[{"id": "c1", "periodStart": 200, "periodEnd": 350}])
    recs = r.by_period(100, 400)
    assert [(x["id"], x["source"]) for x in recs] == [
        ("t1", "turn"), ("c1", "chunk"), ("m1", "memory"), ("t2", "turn")]


def test_by_period_wire_filters():
    cap = []
    recall(cap).by_period(100, 400)
    by_dataset = {b["dataset"]: b for _, _, b in cap}
    assert by_dataset["agent_memory_items"]["objectId"] == "brain1"
    assert by_dataset["agent_memory_items"]["filter"] == {
        "validFrom": {"$gte": 100, "$lte": 400}}
    assert by_dataset["agent_turns"]["objectId"] == "chat1"
    assert by_dataset["agent_turns"]["filter"] == {"createdAt": {"$gte": 100, "$lte": 400}}
    # chunks match on period OVERLAP, not containment
    assert by_dataset["agent_chunks"]["filter"] == {
        "periodStart": {"$lte": 400}, "periodEnd": {"$gte": 100}}


def test_by_period_skips_sources_without_object_id():
    cap = []
    r = build(fake_any(cap), brain_object_id="brain1")
    assert r.by_period(0, 1) == []
    assert [b["dataset"] for _, _, b in cap] == ["agent_memory_items"]  # no chat queries


# --- neighbors ---------------------------------------------------------------

def test_neighbors_forward_refs_only_links_props():
    cap = []
    got = recall(cap).neighbors("obj1")
    assert got["backlinks"] == [{"sourceId": "src9", "typeId": "t1", "propId": "p_ref"}]
    # any:// prefixes stripped — forward and backlinks speak bare ids
    assert sorted(f["targetId"] for f in got["forward"]) == ["target1", "target2", "target3"]
    by_target = {f["targetId"]: f for f in got["forward"]}
    assert by_target["target1"]["propName"] == "Author"
    assert by_target["target2"]["propId"] == "p_refs"
    # p_str (no links format) contributed nothing; reserved any/nav skipped
    assert all(f["propId"] != "p_str" for f in got["forward"])
    assert all(f["typeId"] == "t1" for f in got["forward"])


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
            return _reply(200, {"backlinks": []})
        return _reply(200, {"records": []})
    assert build(fx).neighbors("nope") == {"forward": [], "backlinks": []}


def test_neighbors_tolerates_unqueryable_type_group():
    def fx(name, payload):
        path = payload["url"]
        if path.endswith("/objects/query"):
            return _reply(200, {"records": [{"id": "o", "ghost": {"p": "x"}}]})
        if path.endswith("/backlinks"):
            return _reply(200, {"backlinks": []})
        return _reply(404, {"error": {"code": "type.not_found", "message": "ghost"}})
    assert build(fx).neighbors("o") == {"forward": [], "backlinks": []}


def test_neighbors_backlinks_route_missing_degrades_empty():
    # pre-backlinks server: the route 404s with request.not_found —
    # neighbors still answers with forward refs and empty backlinks
    def fx(name, payload):
        if payload["url"].endswith("/objects/query"):
            return _reply(200, {"records": []})
        return _reply(404, {"error": {"code": "request.not_found", "message": "Not Found"}})
    assert build(fx).neighbors("x") == {"forward": [], "backlinks": []}
