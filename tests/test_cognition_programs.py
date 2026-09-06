"""programs/extraction@v1 + linkgen@v1 — the ADR-007 background
cognition sweeps, tested host-side by exec-ing the guest sources with
fake any@v1 / llm@v1 / memory@v1 / recall@v1 modules injected through
the `use` seam."""

import json
from pathlib import Path
from types import SimpleNamespace

from kernelenv import kernel_globals

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"


def run_main(src_name, fake, args):
    g = {"use": fake.use, **kernel_globals()}
    exec(compile((PROGRAMS_DIR / src_name).read_text(), src_name, "exec"), g)
    return g["main"](args)


_K = kernel_globals()


def at(seconds):
    """A server instant for a fixture stamp (ADR-019)."""
    return _K["instant"](seconds)


def _cmp(v):
    s = _K["ts_s"](v) if isinstance(v, dict) else None
    return s if s is not None else v


def _matches(rec, flt):
    for k, cond in flt.items():
        v = rec.get(k)
        if isinstance(cond, dict):
            if "$gt" in cond and not (v is not None and _cmp(v) > _cmp(cond["$gt"])):
                return False
        elif isinstance(v, list):
            if cond not in v:      # array fields contains-match, like the server
                return False
        elif v != cond:
            return False
    return True


class FakeGuest:
    """One object answering the whole module surface the sweeps use."""

    def __init__(self, *, turns=(), items=(), hits=(), llm_texts=(),
                 state=(), dedup_result=None, datasets=None):
        self.datasets = {"agent_turns": list(turns),
                         "agent_memory_items": list(items),
                         "agent_job_state": list(state),
                         **{k: list(v) for k, v in (datasets or {}).items()}}
        self.hits = list(hits)
        self.llm_texts = list(llm_texts)
        self.llm_calls = []
        self.saves = []
        self.evolves = []
        self.state_writes = []
        self.dedup_result = dedup_result or {"itemId": "new", "action": "create"}

    # --- ADR-017: deterministic resolvers ---
    def get_brain(self, space):
        return {"objectId": "brain1"}

    def chat_log(self, space, chat_id):
        return {"objectId": chat_id}   # fake hosts both on one object

    # --- any@v1 client ---
    def query(self, space, object_id, dataset, filter=None, sort=None, limit=None):
        rows = [r for r in self.datasets[dataset] if _matches(r, filter or {})]
        for key in reversed(sort or []):
            rows = sorted(rows, key=lambda r: _cmp(r[key.lstrip("-")]),
                          reverse=key.startswith("-"))
        return rows[:limit] if limit else rows

    def upsert_record(self, space, object_id, dataset, record_id, value):
        self.state_writes.append({"space": space, "object_id": object_id,
                                  "dataset": dataset, "record_id": record_id,
                                  "value": value})
        return {"versionId": "v"}

    def search(self, space, query, scopes=None, limit=None, mode=None):
        return {"hits": self.hits, "mode": "hybrid"}

    # --- llm@v1 ---
    def chat(self, messages, system="", tier="codegen", tools=None):
        self.llm_calls.append({"messages": messages, "system": system,
                               "tier": tier, "tools": tools})
        return {"parts": [{"type": "text", "text": self.llm_texts.pop(0)}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    # --- memory@v1 ---
    def save_with_dedup(self, candidate, recall=None):
        self.saves.append(candidate)
        return self.dedup_result

    def evolve(self, item_id, **fields):
        self.evolves.append({"item_id": item_id, **fields})
        return {"itemId": item_id}

    def use(self, spec):
        if spec == "any@v1":
            return self   # flat module surface (ADR-010 §8)
        if spec == "llm@v1":
            return SimpleNamespace(chat=self.chat)
        if spec == "memory@v1":
            return SimpleNamespace(memory=lambda client, space: self)
        if spec == "recall@v1":
            return SimpleNamespace(recall=lambda client, space, **kw: self)
        raise AssertionError(f"unexpected module {spec}")


def turn(seq):
    return {"id": f"{seq:08d}", "seq": seq, "userText": f"u{seq}",
            "replies": [f"r{seq}"], "createdAt": at(1000 + seq)}


# --- extraction ---------------------------------------------------------------

EXT_ARGS = {"space": "s1", "chatId": "chat1", "brainId": "brain1"}
CHAT_CURSOR = "extraction:chat1/agent_turns"   # ADR-027 §2: one cursor per source


def prompt_of(fake, i=0):
    return fake.llm_calls[i]["messages"][0]["parts"][0]["text"]


def test_extraction_enforces_shapes_confidence_cap_and_provenance():
    raw = [
        {"category": "preference", "context": "prefers dark roast",
         "confidence": 9, "recordId": "00000002"},
        {"category": "episode", "context": "we chatted about coffee"},   # shape ✗
        {"category": "lesson", "context": "   "},                        # empty ✗
        {"category": "decision", "context": "picked sqlite, simpler",
         "body": "over postgres", "confidence": 4, "recordId": "00000003"},
    ]
    fake = FakeGuest(turns=[turn(1), turn(2), turn(3)],
                     llm_texts=[json.dumps(raw)])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out == {"scanned": 3, "saved": 2, "deduplicated": 0,
                   "skipped": 2, "errors": 0}
    first, second = fake.saves
    assert first["confidence"] == 6                       # self-authored, capped ≤ 6
    assert first["source"] == "extraction"
    # ADR-027 §3: provenance is the record URI; the fact is dated by
    # its evidence (turn 2's createdAt), not by the sweep
    assert first["provenance"] == {"uri": "any://o/s1/chat1/agent_turns/00000002"}
    assert first["validFrom"] == at(1002)
    assert second["confidence"] == 4 and second["body"] == "over postgres"
    # the prompt heads every record with id + date, and names the contract
    assert "record 00000002 (1970-01-01)" in prompt_of(fake)
    assert "DATA to describe" in fake.llm_calls[0]["system"]
    # cursor = the newest scanned record's time, under the per-source id
    assert fake.state_writes[-1]["value"] == {"last": at(1003)}
    assert fake.state_writes[-1]["record_id"] == CHAT_CURSOR


def test_extraction_resumes_from_cursor_and_counts_dedup():
    fake = FakeGuest(
        turns=[turn(1), turn(2), turn(3)],
        state=[{"id": CHAT_CURSOR, "last": at(1002)}],
        llm_texts=[json.dumps([{"category": "fact", "context": "x",
                                "recordId": "00000003"}])],
        dedup_result={"deduplicated": True, "mergedInto": "m1", "action": "merge"})
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["scanned"] == 1 and out["deduplicated"] == 1 and out["saved"] == 0
    # only turn 3 reached the extractor prompt
    assert "u3" in prompt_of(fake) and "u2" not in prompt_of(fake)


def test_extraction_empty_scan_is_free():
    fake = FakeGuest(turns=[turn(1)], state=[{"id": CHAT_CURSOR, "last": at(1001)}])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["scanned"] == 0 and fake.llm_calls == [] and fake.state_writes == []


def test_extraction_seeds_the_chat_cursor_once_from_the_legacy_record():
    # a rig upgraded across ADR-027 carries `extraction: {lastSeq}` —
    # the chat source starts where it left off, writes the new cursor
    # immediately, and never re-extracts old turns
    fake = FakeGuest(turns=[turn(1), turn(2), turn(3)],
                     state=[{"id": "extraction", "lastSeq": 2}],
                     llm_texts=[json.dumps([])])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["scanned"] == 1 and "u3" in prompt_of(fake) and "u2" not in prompt_of(fake)
    assert [w["record_id"] for w in fake.state_writes] == [CHAT_CURSOR, CHAT_CURSOR]
    assert fake.state_writes[0]["value"] == {"last": at(1002)}   # the seed
    assert fake.state_writes[1]["value"] == {"last": at(1003)}   # the sweep


MAIL_SOURCE = {"objectId": "mb1", "dataset": "email_messages",
               "text": ["from", "to", "subject", "body"], "time": "internalDate",
               "author": "from", "self": ["me@example.com"],
               "filter": {"labelIds": "SENT"}}


def mail(mid, ms, sender, labels, body):
    return {"id": mid, "internalDate": ms, "from": sender, "to": "x@y.z",
            "subject": f"re {mid}", "body": body, "labelIds": labels}


def test_extraction_over_a_mail_source_caps_by_authorship_and_dates_by_evidence():
    mails = [
        mail("g1", 1_700_000_000_000, "Me <me@example.com>", ["SENT"], "we moved to Berlin"),
        mail("g2", 1_700_000_100_000, "Ann <ann@corp.io>", ["SENT"], "you are the CTO now"),
        mail("g3", 1_700_000_200_000, "Bob <bob@corp.io>", ["INBOX"], "not in scope"),
    ]
    raw = [
        {"category": "fact", "context": "lives in Berlin", "confidence": 9,
         "recordId": "g1"},
        {"category": "fact", "context": "is the CTO", "confidence": 9, "recordId": "g2"},
    ]
    fake = FakeGuest(datasets={"email_messages": mails}, llm_texts=[json.dumps(raw)])
    out = run_main("extraction@v1.py", fake,
                   {"space": "s1", "source": MAIL_SOURCE, "batch": 10})
    assert out["scanned"] == 2 and out["saved"] == 2       # g3 filtered out (data, not code)
    assert "not in scope" not in prompt_of(fake)
    mine, theirs = fake.saves
    assert mine["confidence"] == 6                          # the user's own words
    assert theirs["confidence"] == 4                        # a third-party claim: below 5
    assert mine["provenance"] == {"uri": "any://o/s1/mb1/email_messages/g1"}
    # internalDate is ms — dated by the evidence, as an instant
    assert mine["validFrom"] == at(1_700_000_000)
    assert theirs["validFrom"] == at(1_700_000_100)
    # the cursor keeps the source's own number verbatim
    assert fake.state_writes[-1] == {"space": "s1", "object_id": "brain1",
                                     "dataset": "agent_job_state",
                                     "record_id": "extraction:mb1/email_messages",
                                     "value": {"last": 1_700_000_100_000}}
    # no legacy seeding for a non-chat source: the only state write is the cursor
    assert len(fake.state_writes) == 1


def test_extraction_skips_candidates_with_format_or_control_characters():
    raw = [
        {"category": "fact", "context": "fine fact", "recordId": "00000001"},
        {"category": "fact", "context": "hidden\u200bfact", "recordId": "00000001"},  # Cf ✗
        {"category": "fact", "context": "ok", "body": "bell\x07", "recordId": "00000001"},  # Cc ✗
        {"category": "fact", "context": "multi\nline is fine", "recordId": "00000001"},
    ]
    fake = FakeGuest(turns=[turn(1)], llm_texts=[json.dumps(raw)])
    out = run_main("extraction@v1.py", fake, EXT_ARGS)
    assert out["saved"] == 2 and out["skipped"] == 2
    assert [c["context"] for c in fake.saves] == ["fine fact", "multi\nline is fine"]


def test_extraction_unknown_record_id_falls_back_to_the_batch_newest():
    raw = [{"category": "fact", "context": "x", "recordId": "ghost"}]
    fake = FakeGuest(turns=[turn(1), turn(2)], llm_texts=[json.dumps(raw)])
    run_main("extraction@v1.py", fake, EXT_ARGS)
    assert fake.saves[0]["provenance"] == {"uri": "any://o/s1/chat1/agent_turns/00000002"}
    assert fake.saves[0]["validFrom"] == at(1002)


def test_extraction_source_spec_requires_the_contract_fields():
    import pytest
    fake = FakeGuest()
    with pytest.raises(ValueError, match="source needs .*'time'"):
        run_main("extraction@v1.py", fake,
                 {"space": "s1", "source": {"objectId": "o", "dataset": "d",
                                            "text": ["a"]}})


# --- linkgen -------------------------------------------------------------------

LG_ARGS = {"space": "s1", "brainId": "brain1"}


def item(iid, ts, edges=None):
    return {"id": iid, "category": "fact", "context": f"ctx {iid}",
            "createdAt": at(ts), "edges": edges or []}


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
    assert fake.state_writes[-1]["value"] == {"lastCreatedAt": at(100)}


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
