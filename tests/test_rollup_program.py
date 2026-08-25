"""programs/rollup@v1 — the hierarchical rollup trigger job (ADR-006
§2), tested host-side by exec-ing the guest source with fake any@v1 /
llm@v1 modules injected through the `use` seam."""

from pathlib import Path
from types import SimpleNamespace

from kernelenv import kernel_globals

_K = kernel_globals()


def at(seconds):
    return _K["instant"](seconds)

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
SRC = (PROGRAMS_DIR / "rollup@v1.py").read_text()


class FakeSpace:
    """Answers the any@v1 client + llm@v1 chat surface like the wire."""

    def __init__(self, turns=(), chunks=()):
        self.turns = list(turns)
        self.chunks = list(chunks)
        self.created = []
        self.llm_calls = []

    def chat(self, messages, system="", tier="codegen", tools=None):
        self.llm_calls.append({"messages": messages, "system": system,
                               "tier": tier, "tools": tools})
        return {"parts": [{"type": "text", "text": f"S{len(self.llm_calls)}"}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    def chat_log(self, space, chat_id):
        # ADR-017: turns/chunks host = the chat's log child; the fake
        # serves both hosts from the same lists, so identity is enough
        return {"objectId": f"{chat_id}-log"}

    def create_chunk(self, space, object_id, body):
        body = dict(body)
        body["seq"] = max((c["seq"] for c in self.chunks), default=0) + 1
        self.chunks.append(body)
        self.created.append(body)
        return {"seq": body["seq"]}

    def query(self, space, object_id, dataset, filter=None, sort=None, limit=None):
        rows = {"agent_turns": self.turns, "agent_chunks": self.chunks}[dataset]
        rows = [r for r in rows if _matches(r, filter or {})]
        for key in reversed(sort or []):
            rev = key.startswith("-")
            rows = sorted(rows, key=lambda r: r[key.lstrip("-")], reverse=rev)
        return rows[:limit] if limit else rows

    def use(self, spec):
        if spec == "any@v1":
            return self   # flat module surface (ADR-010 §8)
        if spec == "llm@v1":
            return SimpleNamespace(chat=self.chat)
        raise AssertionError(f"unexpected module {spec}")


def _matches(rec, flt):
    for k, cond in flt.items():
        v = rec.get(k)
        if isinstance(cond, dict):
            if "$gt" in cond and not (v is not None and v > cond["$gt"]):
                return False
        elif v != cond:
            return False
    return True


def run_main(fake, args):
    g = {"use": fake.use, **kernel_globals()}
    exec(compile(SRC, "rollup@v1.py", "exec"), g)
    return g["main"](args)


def turn(seq, ts=None):
    return {"seq": seq, "userText": f"u{seq}", "replies": [f"r{seq}"],
            "createdAt": at(ts if ts is not None else 1000 + seq)}


ARGS = {"space": "s1", "chatId": "chat1"}


def test_l1_rolls_complete_batches_only():
    fake = FakeSpace(turns=[turn(i) for i in range(1, 26)])
    run_main(fake, ARGS)
    l1 = [c for c in fake.created if c["level"] == 1]
    assert [(c["fromSeq"], c["toSeq"]) for c in l1] == [(1, 10), (11, 20)]
    assert all(c["unitsCovered"] == 10 for c in l1)
    # period from the covered turns' timestamps
    # the period is the turns' own instants, verbatim (ADR-019 §2)
    assert (l1[0]["periodStart"], l1[0]["periodEnd"]) == (at(1001), at(1010))
    # partial tail 21-25 untouched; no L2 (only 2 L1 chunks)
    assert all(c["level"] == 1 for c in fake.created)


def test_l1_resumes_after_existing_coverage():
    fake = FakeSpace(
        turns=[turn(i) for i in range(1, 21)],
        chunks=[{"seq": 1, "level": 1, "fromSeq": 1, "toSeq": 10,
                 "summary": "old", "periodStart": 0, "periodEnd": 0}])
    run_main(fake, ARGS)
    assert [(c["fromSeq"], c["toSeq"]) for c in fake.created] == [(11, 20)]


def test_no_complete_batch_no_llm_calls():
    fake = FakeSpace(turns=[turn(i) for i in range(1, 6)])
    run_main(fake, ARGS)
    assert fake.created == [] and fake.llm_calls == []


def test_l2_summarizes_child_summaries_only():
    chunks = [{"seq": i, "level": 1, "fromSeq": i * 10 - 9, "toSeq": i * 10,
               "summary": f"c{i}", "periodStart": at(100 + i), "periodEnd": at(200 + i)}
              for i in range(1, 11)]
    fake = FakeSpace(chunks=chunks)
    run_main(fake, ARGS)
    l2 = [c for c in fake.created if c["level"] == 2]
    assert len(l2) == 1
    # range is over CHILD CHUNK seqs, not turn seqs
    assert (l2[0]["fromSeq"], l2[0]["toSeq"]) == (1, 10)
    assert (l2[0]["periodStart"], l2[0]["periodEnd"]) == (at(101), at(210))
    # the L2 prompt contains child summaries, no raw turn text
    prompt = fake.llm_calls[-1]["messages"][0]["parts"][0]["text"]
    assert "- c1" in prompt and "user:" not in prompt


def test_custom_batch_and_tier_flow_through():
    fake = FakeSpace(turns=[turn(i) for i in range(1, 7)])
    run_main(fake, {**ARGS, "batch": 3, "tier": "cheap"})
    assert [(c["fromSeq"], c["toSeq"]) for c in fake.created] == [(1, 3), (4, 6)]
    assert all(c["tier"] == "cheap" for c in fake.llm_calls)
