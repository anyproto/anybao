"""Offline recall tests — fake transport into a real AnyClient, asserting
the three axes' shapes and that scopes/filters hit the wire correctly."""

from anybao.anyclient import AnyClient
from anybao.recall import Recall

HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
       "recordId": "m1", "score": 0.9}

# One object row with a user type t1: p_ref / p_refs are links-format
# props (the object-ref convention — arrays of any:// URIs; p_ref
# exercises scalar tolerance), p_str a plain string; `any` and `nav` are
# reserved and must be skipped.
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


def fake_any(capture, *, memory=(), turns=(), chunks=()):
    def send(method, path, body):
        capture.append((method, path, body))
        if path.endswith("/search"):
            return 200, {"hits": [HIT], "mode": "hybrid", "vectorStatus": "used"}
        if path.endswith("/objects/query"):
            return 200, {"records": [OBJ_ROW]}
        if path.endswith("/types/t1/properties"):
            return 200, {"properties": T1_PROPS}
        if path.endswith("/backlinks"):
            return 200, {"backlinks": [
                {"objectId": "src9", "typeId": "t1", "propId": "p_ref"}]}
        if path.endswith("/query"):  # per-object dataset query
            recs = {"agent_memory_items": list(memory), "agent_turns": list(turns),
                    "agent_chunks": list(chunks)}[body["dataset"]]
            return 200, {"records": recs}
        return 404, {"error": {"code": "unknown", "message": path}}
    return send


def recall(capture, **fake_kw):
    client = AnyClient(fake_any(capture, **fake_kw))
    return Recall(client, "s1", brain_object_id="brain1", chat_object_id="chat1")


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
    r = Recall(AnyClient(fake_any(cap)), "s1", brain_object_id="brain1")
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
    def send(method, path, body):
        if path.endswith("/backlinks"):
            return 200, {"backlinks": []}
        return 200, {"records": []}
    r = Recall(AnyClient(send), "s1")
    assert r.neighbors("nope") == {"forward": [], "backlinks": []}


def test_neighbors_tolerates_unqueryable_type_group():
    def send(method, path, body):
        if path.endswith("/objects/query"):
            return 200, {"records": [{"id": "o", "ghost": {"p": "x"}}]}
        if path.endswith("/backlinks"):
            return 200, {"backlinks": []}
        return 404, {"error": {"code": "type.not_found", "message": "ghost"}}
    r = Recall(AnyClient(send), "s1")
    assert r.neighbors("o") == {"forward": [], "backlinks": []}


def test_neighbors_backlinks_route_missing_degrades_empty():
    # pre-backlinks server: the route 404s with request.not_found —
    # neighbors still answers with forward refs and empty backlinks
    def send(method, path, body):
        if path.endswith("/objects/query"):
            return 200, {"records": []}
        return 404, {"error": {"code": "request.not_found", "message": "Not Found"}}
    r = Recall(AnyClient(send), "s1")
    assert r.neighbors("x") == {"forward": [], "backlinks": []}
