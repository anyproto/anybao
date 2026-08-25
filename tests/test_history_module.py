"""programs/history@v1 — build_turn + boot-window composition, tested
host-side by exec-ing the guest source (the module is pure except the
client-taking thin reads, so no guest globals are needed)."""

from pathlib import Path

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"


def _load():
    # recent_turns / chunks_at_level are @span-wrapped now (the rest is pure)
    g: dict = {"span": lambda name=None, kind=None: (lambda f: f)}
    exec(compile((PROGRAMS_DIR / "history@v1" / "program.py").read_text(),
                 "history@v1.py", "exec"), g)
    return g


H = _load()


def outcome(replies, stop="done"):
    return {"replies": replies, "stop": stop}


def test_build_turn_replies_vs_think_and_neutral_stop():
    t = H["build_turn"](user_text="hi", outcome=outcome(["hello"]),
                        think="user greets", trace_ref="run1")
    assert t["userText"] == "hi"
    assert t["replies"] == ["hello"]        # what the user saw
    assert t["think"] == "user greets"      # narration NOT in chat (distinct)
    assert t["traceRef"] == "run1"
    assert t["llm"]["stopReason"] == "done"  # neutral outcome, not raw provider
    assert t["interrupted"] is False
    assert "seq" not in t                    # server-assigned


def test_build_turn_interrupted_on_hard_break():
    t = H["build_turn"](user_text="x",
                        outcome=outcome(["stopped"], stop="break_hard"))
    assert t["interrupted"] is True
    assert t["llm"]["stopReason"] == "break_hard"


def _turn(seq, ut, replies):
    return {"seq": seq, "userText": ut, "replies": replies}


def _chunk(seq, level, frm, to, summary):
    return {"seq": seq, "level": level, "fromSeq": frm, "toSeq": to, "summary": summary}


def test_boot_window_newest_raw_turns_included():
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(10)]
    msgs = H["render_boot_window"](turns, {}, total_tokens=10_000)
    texts = [p["text"] for m in msgs for p in m["parts"]]
    assert "q9" in " ".join(texts)  # newest present
    # oldest→newest ordering of the raw window
    assert texts.index("q8") < texts.index("q9")


def test_boot_window_chunks_fill_after_raw_ascending_level():
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(90, 100)]  # newest 10
    chunks = {
        1: [_chunk(s, 1, s * 10, s * 10 + 9, f"L1 sum {s}") for s in range(9)],
        2: [_chunk(0, 2, 0, 8, "L2 super")],
    }
    msgs = H["render_boot_window"](turns, chunks, total_tokens=4000)
    joined = " ".join(p["text"] for m in msgs for p in m["parts"])
    assert "earlier context, compressed" in joined
    assert "chunk #" in joined                       # drill-down handles present
    assert "L1 sum 8" in joined                       # newest L1 chunk


def test_boot_window_budget_respected():
    turns = [_turn(i, "q" * 200, ["a" * 200]) for i in range(50)]
    chunks = {1: [_chunk(s, 1, s, s, "s" * 100) for s in range(50)]}
    msgs = H["render_boot_window"](turns, chunks, total_tokens=1000)
    total = sum(len(p["text"]) for m in msgs for p in m["parts"])
    # ~4 chars/token budget of 1000 → well under 1000*4*1.5 slack
    assert total < 1000 * 4 * 2


def test_boot_window_skips_double_covered_l1_chunk():
    # raw tail covers seq 5..9; an L1 chunk over 5..9 must NOT also appear
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(5, 10)]
    chunks = {1: [_chunk(0, 1, 0, 4, "old range"), _chunk(1, 1, 5, 9, "covered range")]}
    msgs = H["render_boot_window"](turns, chunks, total_tokens=10_000)
    joined = " ".join(p["text"] for m in msgs for p in m["parts"])
    assert "old range" in joined            # older chunk kept
    assert "covered range" not in joined    # double-covered chunk skipped


def test_raw_tail_min_seq_is_the_guard_boundary():
    turns = [_turn(i, "q" * 400, ["a" * 400]) for i in range(10)]
    tail = H["raw_tail"](turns, total_tokens=1000, raw_tail_fraction=0.5)
    assert tail and tail[-1]["seq"] == 9            # newest kept
    assert tail == turns[tail[0]["seq"]:]           # a contiguous newest slice


def test_approx_tokens_four_chars_per_token():
    assert H["approx_tokens"]("") == 0
    assert H["approx_tokens"]("abcd") == 1
    assert H["approx_tokens"]("abcde") == 2


class FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, space, object_id, dataset, **body):
        self.queries.append((space, object_id, dataset, body))
        return [{"seq": 1}]

    def chat_log(self, space, chat_id):
        # ADR-017: the log child hosts turns/chunks; identity suffices
        return {"objectId": chat_id}


def test_recent_turns_and_chunks_at_level_thin_reads():
    c = FakeClient()
    assert H["recent_turns"](c, "s1", "chat1", 5) == [{"seq": 1}]
    assert c.queries[0] == ("s1", "chat1", "agent_turns",
                            {"sort": ["-seq"], "limit": 5})
    H["chunks_at_level"](c, "s1", "chat1", 2, 7)
    assert c.queries[1] == ("s1", "chat1", "agent_chunks",
                            {"filter": {"level": 2}, "sort": ["-seq"], "limit": 7})


def test_activity_groups_turns_by_calendar_period_over_the_log():
    # ADR-019 §3: native date arithmetic ($dateTrunc) over the turns'
    # createdAt instants, run on the chat's log child
    calls = []

    class Client:
        def chat_log(self, space, chat_id):
            return {"objectId": f"{chat_id}-log"}

        def aggregate(self, space, pipeline, object_id=None, dataset=None):
            calls.append((space, pipeline, object_id, dataset))
            return {"records": [{"id": {"$date": "2026-08-24T00:00:00Z"}, "turns": 3},
                                {"id": {"$date": "2026-08-25T00:00:00Z"}, "turns": 1}]}

    out = H["activity"](Client(), "s1", "chat1", unit="day")
    assert out == [{"period": {"$date": "2026-08-24T00:00:00Z"}, "turns": 3},
                   {"period": {"$date": "2026-08-25T00:00:00Z"}, "turns": 1}]
    space, pipeline, oid, ds = calls[0]
    assert (space, oid, ds) == ("s1", "chat1-log", "agent_turns")
    assert pipeline[0] == {"$group": {
        "_id": {"$dateTrunc": {"date": "$createdAt", "unit": "day"}},
        "turns": {"$count": {}}}}
    assert pipeline[1] == {"$sort": {"id": 1}}
