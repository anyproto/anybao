"""programs/extraction@v1 + linkgen@v1 — the ADR-007 background
cognition sweeps, tested host-side by exec-ing the guest sources with a
fake `effect` global."""

import json
from pathlib import Path

from anybao.deploy import load_programs
from anybao.memory import extraction_trigger, linkgen_trigger

PROGRAMS_DIR = Path(__file__).resolve().parents[2] / "programs"


def run_main(src_name, fake, args):
    g = {"effect": fake}
    exec(compile((PROGRAMS_DIR / src_name).read_text(), src_name, "exec"), g)
    return g["main"](args)


def _matches(rec, flt):
    for k, cond in flt.items():
        v = rec.get(k)
        if isinstance(cond, dict):
            if "$gt" in cond and not (v is not None and v > cond["$gt"]):
                return False
        elif v != cond:
            return False
    return True


class FakeGuest:
    def __init__(self, *, turns=(), items=(), hits=(), llm_texts=(),
                 state=(), dedup_result=None):
        self.datasets = {"agent_turns": list(turns),
                         "agent_memory_items": list(items),
                         "agent_job_state": list(state)}
        self.hits = list(hits)
        self.llm_texts = list(llm_texts)
        self.llm_calls = []
        self.saves = []
        self.evolves = []
        self.state_writes = []
        self.dedup_result = dedup_result or {"itemId": "new", "action": "create"}

    def __call__(self, name, payload):
        if name == "any.query":
            rows = [r for r in self.datasets[payload["dataset"]]
                    if _matches(r, payload.get("filter") or {})]
            for key in reversed(payload.get("sort") or []):
                rows = sorted(rows, key=lambda r: r[key.lstrip("-")],
                              reverse=key.startswith("-"))
            lim = payload.get("limit")
            return rows[:lim] if lim else rows
        if name == "any.upsert_record":
            self.state_writes.append(payload)
            return {"versionId": "v"}
        if name == "any.search":
            return {"hits": self.hits, "mode": "hybrid"}
        if name == "llm.chat":
            self.llm_calls.append(payload)
            return {"parts": [{"type": "text", "text": self.llm_texts.pop(0)}],
                    "stop": "done", "usage": {"in": 1, "out": 1}}
        if name == "memory.save_with_dedup":
            self.saves.append(payload["candidate"])
            return self.dedup_result
        if name == "memory.evolve":
            self.evolves.append(payload)
            return {"itemId": payload["item_id"]}
        raise AssertionError(f"unexpected effect {name}")


def turn(seq):
    return {"seq": seq, "userText": f"u{seq}", "replies": [f"r{seq}"],
            "createdAt": 1000 + seq}


# --- extraction ---------------------------------------------------------------

EXT_ARGS = {"space": "s1", "chatId": "chat1", "brainId": "brain1"}


def test_extraction_enforces_shapes_confidence_cap_and_provenance():
    raw = [
        {"category": "preference", "context": "prefers dark roast",
         "confidence": 9, "fromSeq": 2},
        {"category": "episode", "context": "we chatted about coffee"},   # shape ✗
        {"category": "lesson", "context": "   "},                        # empty ✗
        {"category": "decision", "context": "picked sqlite, simpler",
         "body": "over postgres", "confidence": 4, "fromSeq": 3},
    ]
    fake = FakeGuest(turns=[turn(1), turn(2), turn(3)],
                     llm_texts=[json.dumps(raw)])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out == {"scanned": 3, "saved": 2, "deduplicated": 0,
                   "skipped": 2, "errors": 0}
    first, second = fake.saves
    assert first["confidence"] == 6                       # capped ≤ 6
    assert first["provenance"] == {"fromSeq": 2}          # provenance kept
    assert first["source"] == "extraction"
    assert second["confidence"] == 4 and second["body"] == "over postgres"
    # cursor advanced to the last scanned turn
    assert fake.state_writes[-1]["value"] == {"lastSeq": 3}
    assert fake.state_writes[-1]["record_id"] == "extraction"


def test_extraction_resumes_from_cursor_and_counts_dedup():
    fake = FakeGuest(
        turns=[turn(1), turn(2), turn(3)],
        state=[{"id": "extraction", "lastSeq": 2}],
        llm_texts=[json.dumps([{"category": "fact", "context": "x", "fromSeq": 3}])],
        dedup_result={"deduplicated": True, "mergedInto": "m1", "action": "merge"})
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["scanned"] == 1 and out["deduplicated"] == 1 and out["saved"] == 0
    # only turn 3 reached the extractor prompt
    assert "u3" in fake.llm_calls[0]["messages"][0]["parts"][0]["text"]
    assert "u2" not in fake.llm_calls[0]["messages"][0]["parts"][0]["text"]


def test_extraction_empty_scan_is_free():
    fake = FakeGuest(turns=[turn(1)], state=[{"id": "extraction", "lastSeq": 1}])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["scanned"] == 0 and fake.llm_calls == [] and fake.state_writes == []


# --- linkgen -------------------------------------------------------------------

LG_ARGS = {"space": "s1", "brainId": "brain1"}


def item(iid, ts, edges=None):
    return {"id": iid, "category": "fact", "context": f"ctx {iid}",
            "createdAt": ts, "edges": edges or []}


def test_linkgen_writes_only_curated_vocabulary_to_real_neighbors():
    proposals = [
        {"to": "m2", "type": "relates_to"},     # ok
        {"to": "m2", "type": "invented_type"},  # vocabulary ✗
        {"to": "ghost", "type": "caused_by"},   # not a neighbor ✗
        {"to": "m2", "type": "relates_to"},     # duplicate ✗
    ]
    fake = FakeGuest(
        items=[item("m1", 100)],
        hits=[{"recordId": "m2", "scope": "agent", "snippet": "other fact"},
              {"recordId": "m1", "scope": "agent"}],     # self-hit dropped
        llm_texts=[json.dumps(proposals)])
    out = run_main("linkgen@v1.py", fake, LG_ARGS)
    assert out == {"swept": 1, "linked": 1, "errors": 0}
    assert fake.evolves == [{"item_id": "m1",
                             "edges": [{"to": "m2", "type": "relates_to"}]}]
    # self was not offered as a neighbor
    assert "id=m1" not in fake.llm_calls[0]["messages"][0]["parts"][0]["text"]
    assert fake.state_writes[-1]["value"] == {"lastCreatedAt": 100}


def test_linkgen_appends_to_existing_edges_and_respects_cursor():
    fake = FakeGuest(
        items=[item("m1", 50), item("m2", 150,
                                    edges=[{"to": "m9", "type": "part_of"}])],
        state=[{"id": "linkgen", "lastCreatedAt": 100}],
        hits=[{"recordId": "m7", "scope": "agent", "snippet": "s"}],
        llm_texts=[json.dumps([{"to": "m7", "type": "discussed_in"}])])
    out = run_main("linkgen@v1.py", fake, LG_ARGS)
    assert out["swept"] == 1     # m1 is behind the cursor
    assert fake.evolves[0]["edges"] == [{"to": "m9", "type": "part_of"},
                                        {"to": "m7", "type": "discussed_in"}]


def test_linkgen_no_neighbors_no_llm_call():
    fake = FakeGuest(items=[item("m1", 100)],
                     hits=[{"recordId": "m1", "scope": "agent"}])  # only self
    out = run_main("linkgen@v1.py", fake, LG_ARGS)
    assert out == {"swept": 1, "linked": 0, "errors": 0}
    assert fake.llm_calls == []


def test_llm_parse_failure_counts_error_and_sweep_continues():
    fake = FakeGuest(
        items=[item("m1", 100), item("m2", 110)],
        hits=[{"recordId": "m7", "scope": "agent", "snippet": "s"}],
        llm_texts=["no json here", json.dumps([{"to": "m7", "type": "part_of"}])])
    out = run_main("linkgen@v1.py", fake, LG_ARGS)
    assert out == {"swept": 2, "linked": 1, "errors": 1}


# --- factories + deploy pipeline ----------------------------------------------

def test_programs_load_via_deploy_pipeline():
    specs = {p.spec for p in load_programs(PROGRAMS_DIR)}
    assert {"extraction@v1", "linkgen@v1", "rollup@v1"} <= specs


def test_trigger_factories():
    e = extraction_trigger(space="s1", chat_id="c1", brain_id="b1", owner="i1")
    assert (e.program, e.kind, e.spec) == ("extraction@v1", "cron", {"every_s": 900})
    assert e.args == {"space": "s1", "chatId": "c1", "brainId": "b1"}
    lg = linkgen_trigger(space="s1", brain_id="b1")
    assert (lg.program, lg.spec) == ("linkgen@v1", {"every_s": 3600})
    assert lg.args == {"space": "s1", "brainId": "b1"}
