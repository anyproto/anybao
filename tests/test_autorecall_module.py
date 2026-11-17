"""programs/autorecall@v1 (ADR-007 §5) — offline: fake recall/memory
modules injected through the guest `use` seam, asserting framing,
guards, caps, the accessCount bump, and the ROI log."""

from pathlib import Path
from types import SimpleNamespace

from kernelenv import kernel_globals

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
SRC = (PROGRAMS_DIR / "autorecall@v1.py").read_text()

MEM_HIT = {"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
           "key": "agent_memory_items",
           "recordId": "m1", "score": 1.0}
TURN_HIT = {"scope": "history", "objectId": "chat1", "dataset": "agent_turns",
            "key": "agent_turns",
            "recordId": "t1", "score": 0.954}
CHUNK_HIT = {"scope": "history", "objectId": "chat1", "dataset": "agent_chunks",
             "key": "agent_chunks",
             "recordId": "c1", "score": 0.913}

MEM_REC = {"id": "m1", "category": "preference", "context": "prefers dark roast",
           "confidence": 8, "validFrom": {"$date": 1751328000000},        # 2025-07-01
           "accessCount": 2}
TURN_REC = {"id": "t1", "seq": 4, "userText": "let's plan the trip\nmore",
            "createdAt": {"$date": "2025-07-01T00:00:00.000Z"}}
CHUNK_REC = {"id": "c1", "seq": 1, "level": 1, "fromSeq": 1, "toSeq": 3,
             "summary": "planned the trip", "periodStart": {"$date": 1751328000000}}


# the reply envelopes the gate tells apart (ADR-007 §5)
UNIT = {"mode": "hybrid", "scoreScale": "unit", "vectorStatus": "used"}
RAW = {"mode": "hybrid", "vectorStatus": "used"}        # a server before any#335
FTS = {"mode": "fts", "vectorStatus": "disabled"}       # no embedder / loading


class FakeRecall:
    def __init__(self, hits, recs, fail=False, envelope=UNIT):
        self.hits = list(hits)
        self.envelope = envelope
        self.recs = recs
        self.fail = fail
        self.searches = []
        self.hydrations = 0

    def search_reply(self, query, scopes=None, limit=None):
        if self.fail:
            raise OSError("network down")
        self.searches.append({"query": query, "scopes": scopes, "limit": limit})
        return {**self.envelope, "hits": self.hits}

    def hydrate(self, hits):
        self.hydrations += 1
        out = []
        for h in hits:
            for r in self.recs.get(h["dataset"], []):
                if r["id"] == h["recordId"]:
                    out.append((h, r))
        return out


class FakeMemory:
    def __init__(self):
        self.bumps = []

    def bump_access(self, item_id, current_count):
        self.bumps.append({"item_id": item_id, "accessCount": current_count + 1})
        return {"itemId": item_id, "accessCount": current_count + 1}


def load(hits, recs=None, fail=False, envelope=UNIT):
    rec = FakeRecall(hits, recs or {
        "agent_memory_items": [MEM_REC], "agent_turns": [TURN_REC],
        "agent_chunks": [CHUNK_REC]}, fail=fail, envelope=envelope)
    mem = FakeMemory()
    modules = {
        "recall@v1": SimpleNamespace(recall=lambda client, space, **kw: rec),
        "memory@v1": SimpleNamespace(memory=lambda client: mem),
    }
    g = {**kernel_globals(), "use": lambda spec: modules[spec],
         "span": lambda name=None, kind=None, label=None: (lambda fn: fn)}
    exec(compile(SRC, "autorecall@v1.py", "exec"), g)
    return g, rec, mem


def test_injects_tool_framed_pair_with_both_sections():
    g, _, _ = load([MEM_HIT, TURN_HIT, CHUNK_HIT])
    out = g["plan"](None, "s1", "trip?")
    msgs = out["messages"]
    assert [m["role"] for m in msgs] == ["assistant", "user"]
    call, result = msgs[0]["parts"][0], msgs[1]["parts"][0]
    # framed as run_cell — the loop's only declared tool; the code is
    # the real recall idiom (an invented tool name reads as a failed
    # call the model apologizes for)
    assert call["type"] == "tool_call" and call["name"] == "run_cell"
    assert call["id"] == "autorecall_0"
    code = call["args"]["code"]
    assert 'use("agent:recall@v1").recall(use("agent:any@v1"), baoSpaceConfig)' in code
    assert "rec.hydrate(rec.search('trip?', scopes=['agent', 'history']))" in code
    assert result["type"] == "tool_result" and result["call_id"] == call["id"]
    assert result["content"].startswith("Last value: [")
    assert "values.get" not in result["content"]  # nothing stored under this id
    assert "Memories:" in result["content"] and "Related history:" in result["content"]
    assert "[preference] prefers dark roast (saved 2025-07-01, confidence 8)" \
        in result["content"]
    assert "[chunk #1 (L1), turns 1–3]" in result["content"]
    assert "“let's plan the trip” [turn #4]" in result["content"]
    # injected = the memory pairs, the ROI log's input
    assert out["injected"] == [(MEM_HIT, MEM_REC)]


def test_generic_message_below_threshold_injects_nothing():
    # rank 4 on both legs, and rank 2 of a single-leg form
    weak = [{**MEM_HIT, "score": 0.875}, {**TURN_HIT, "score": 0.5}]
    g, rec, _ = load(weak)
    assert g["plan"](None, "s1", "hi") == {"messages": [], "injected": []}
    # threshold cut happens before any hydration round-trip
    assert rec.hydrations == 0


def test_raw_rrf_reply_gates_on_the_raw_floor():
    # hybrid with no scoreScale = the raw RRF sum at k 60
    hits = [{**MEM_HIT, "score": 0.033}, {**TURN_HIT, "score": 0.011}]
    g, _, _ = load(hits, envelope=RAW)
    out = g["plan"](None, "s1", "coffee?")
    content = out["messages"][1]["parts"][0]["content"]
    assert "prefers dark roast" in content and "Related history:" not in content
    assert [h["recordId"] for h, _ in out["injected"]] == ["m1"]


def test_non_hybrid_reply_injects_nothing():
    # BM25 scores clear any floor, and mean nothing across queries
    hits = [{**MEM_HIT, "score": 7.4}, {**TURN_HIT, "score": 3.1}]
    for envelope in (FTS, {"mode": "vector"}, {}):
        g, rec, mem = load(hits, envelope=envelope)
        assert g["plan"](None, "s1", "coffee?") == {"messages": [], "injected": []}
        assert rec.hydrations == 0 and mem.bumps == []


def test_unknown_score_scale_injects_nothing():
    g, rec, _ = load([MEM_HIT], envelope={"mode": "hybrid", "scoreScale": "z"})
    assert g["plan"](None, "s1", "coffee?") == {"messages": [], "injected": []}
    assert rec.hydrations == 0


def test_title_hit_never_passes_the_gate():
    # a title hit keeps BM25 in a hybrid reply, in the same hits list
    title = {**MEM_HIT, "score": 12.0, "match": "title"}
    g, rec, _ = load([title])
    assert g["plan"](None, "s1", "coffee?") == {"messages": [], "injected": []}
    assert rec.hydrations == 0


def test_injected_memory_items_bump_access_count():
    g, _, mem = load([MEM_HIT])
    g["plan"](None, "s1", "coffee?")
    assert mem.bumps == [{"item_id": "m1", "accessCount": 3}]  # 2 + 1


def test_deep_history_guard_skips_boot_covered_hits():
    g, _, _ = load([TURN_HIT, CHUNK_HIT])
    # raw tail starts at seq 4 → turn #4 covered; chunk 1–3 is deeper, stays
    msgs = g["plan"](None, "s1", "trip?", boot_min_seq=4)["messages"]
    content = msgs[1]["parts"][0]["content"]
    assert "turn #4" not in content and "chunk #1" in content
    # chunk fully inside the tail is covered too
    assert g["covered_by_boot"](CHUNK_REC, "agent_chunks", 1)


class ChunkClient:
    """query() over a fixed chunk list — the level-1 children."""

    def __init__(self, chunks):
        self.chunks = chunks

    def query(self, space, object_id, dataset, filter=None, limit=None):
        return [c for c in self.chunks
                if all(c.get(k) == v for k, v in (filter or {}).items())]


def test_level2_chunk_range_is_child_chunks_not_turns():
    # L2 chunk #11 covers L1 chunks 1–10; L1 chunk #1 starts at turn 1
    l2 = {"id": "c1", "seq": 11, "level": 2, "fromSeq": 1, "toSeq": 10,
          "summary": "a month of trips", "periodStart": {"$date": 1751328000000}}
    g, _, _ = load([CHUNK_HIT], {"agent_chunks": [l2]})
    assert "[chunk #11 (L2), chunks 1–10]" in g["history_line"](l2, "agent_chunks")

    kids = ChunkClient([{"seq": 1, "level": 1, "fromSeq": 1, "toSeq": 10}])
    # the raw tail starts at turn 5: chunk seq 1 >= 5 is false AND turn 1
    # is older than the tail, so the hit stays
    msgs = g["plan"](kids, "s1", "trip?", boot_min_seq=5)["messages"]
    assert "chunk #11" in msgs[1]["parts"][0]["content"]
    # a tail that starts at turn 1 holds every turn the chunk covers
    assert g["plan"](kids, "s1", "trip?", boot_min_seq=1)["messages"] == []

    # the bug: child chunk seq 45 read as a turn seq is "older than the
    # tail" although the chunk's turns (431…) all sit inside it
    late = {**l2, "seq": 55, "fromSeq": 45, "toSeq": 54}
    kids = ChunkClient([{"seq": 45, "level": 1, "fromSeq": 431, "toSeq": 440}])
    assert g["first_turn"](kids, "s1", CHUNK_HIT, late) == 431
    assert g["covered_by_boot"](late, "agent_chunks", 400, 431)
    assert not g["covered_by_boot"](late, "agent_chunks", 400, 45)   # the old reading
    # a missing child leaves the span unknown: not covered
    assert g["first_turn"](ChunkClient([]), "s1", CHUNK_HIT, late) is None
    assert not g["covered_by_boot"](late, "agent_chunks", 400)


def test_memory_cap_and_budget_respected():
    hits = [{**MEM_HIT, "recordId": f"m{i}"} for i in range(8)]
    recs = {"agent_memory_items": [
        {**MEM_REC, "id": f"m{i}", "context": f"fact {i}"} for i in range(8)]}
    g, _, _ = load(hits, recs)
    msgs = g["plan"](None, "s1", "q?")["messages"]
    content = msgs[1]["parts"][0]["content"]
    assert content.count("- [preference]") == 5  # max_memory
    g2, _, _ = load(hits, recs)
    msgs2 = g2["plan"](None, "s1", "q?", policy={"token_budget": 15})["messages"]
    assert msgs2[1]["parts"][0]["content"].count("- [preference]") == 1


def test_search_failure_fails_open():
    g, _, _ = load([MEM_HIT], fail=True)
    assert g["plan"](None, "s1", "q") == {"messages": [], "injected": []}


def test_date_helper_civil_from_epoch():
    g, _, _ = load([])
    assert g["_date"](0) == "undated"          # zero = missing
    assert g["_date"](86400) == "1970-01-02"
    assert g["_date"](1751328000) == "2025-07-01"
    assert g["_date"]({"$date": "2025-07-01T23:30:00Z"}) == "2025-07-01"   # host zone UTC
    assert g["_date"]({"$date": 1751328000000}) == "2025-07-01"
    assert g["_date"]("soon") == "undated"
    assert g["_date"]({"$date": "garbage"}) == "undated"


# --- ROI log ------------------------------------------------------------------

def test_referenced_heuristic():
    g, _, _ = load([])
    assert g["referenced"]("prefers dark roast coffee", ["Ordered the dark ROAST."])
    assert not g["referenced"]("prefers dark roast coffee", ["Done!"])
    assert not g["referenced"]("", ["anything"])


class FakeWriter:
    def __init__(self):
        self.writes = []

    def upsert_record(self, space, object_id, dataset, record_id, value):
        self.writes.append({"space": space, "object_id": object_id,
                            "dataset": dataset, "record_id": record_id,
                            "value": value})
        return {"versionId": "v"}


def test_log_roi_writes_one_record_per_injected_pair():
    g, _, _ = load([])
    client = FakeWriter()
    injected = [({"objectId": "brain"}, {"id": "m1", "context": "dark roast coffee"}),
                ({"objectId": "brain"}, {"id": "m2", "context": "sqlite tracker"})]
    n = g["log_roi"](client, "s1", injected, ["I'll get the dark roast."], 1000)
    assert n == 2
    first, second = client.writes
    assert first["object_id"] == "brain"                 # the hit's objectId
    assert first["dataset"] == "agent_roi_injections"
    assert first["record_id"] == "m1:1000"
    assert first["value"] == {"itemId": "m1", "ts": 1000, "referenced": True}
    assert second["record_id"] == "m2:1000"
    assert second["value"] == {"itemId": "m2", "ts": 1000, "referenced": False}
