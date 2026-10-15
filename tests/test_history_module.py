"""programs/history@v1 — build_turn + boot-window composition, tested
host-side by exec-ing the guest source (the module is pure except the
client-taking thin reads, so no guest globals are needed)."""

import json
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


def _sized(seq, chars=1000):
    # ~1000 chars per exchange ≈ 400 estimated tokens, like the real chat
    return _turn(seq, "q" * (chars // 4), ["a" * (chars * 3 // 4)])


def test_raw_tail_is_every_turn_from_the_anchor():
    turns = [_sized(i) for i in range(1, 11)]
    assert H["raw_tail"](turns, 7) == turns[6:]
    assert H["raw_tail"](turns, None) == []


def test_anchor_is_kept_under_high_water_and_jumps_whole_chunks_past_it():
    # ADR-006 §2 hysteresis: 10 turns ≈ 4k tokens per L1 chunk
    turns = [_sized(i) for i in range(1, 61)]
    l1 = [_chunk(n, 1, n * 10 - 9, n * 10, f"L1 #{n}") for n in range(1, 7)]
    high, low = 10_000, 5_000
    # a fresh chat: the oldest boundary whose tail fits under low-water
    a = H["choose_anchor"](turns, l1, None, high, low)
    assert a == 51                                      # turns 51–60 ≈ 4k < 5k
    # kept while the tail (from 41: ~8k) stays under high-water …
    assert H["choose_anchor"](turns, l1, 41, high, low) == 41
    # … and jumps forward, whole chunks, once it crosses (from 31: ~12k)
    assert H["choose_anchor"](turns, l1, 31, high, low) == 51
    # never backwards, even when nothing fits under low-water
    assert H["choose_anchor"](turns, [], 58, 100, 50) >= 58
    # one giant newest turn: the tail is that turn
    giant = turns[:-1] + [_sized(60, 100_000)]
    assert H["choose_anchor"](giant, l1, None, high, low) == 60
    # an anchor older than the fetched turns forces a jump (fetch limit)
    assert H["choose_anchor"](turns[20:], l1, 5, high, low) >= 21
    assert H["choose_anchor"]([], l1, 5, high, low) is None


def test_water_marks_scale_down_for_a_small_budget():
    assert H["water_marks"](40_000) == (25_000, 12_000)
    assert H["water_marks"](8_192) == (5_120, 2_457)


def _rollup(turns, batch=10):
    """rollup@v1 in miniature: L1 per `batch` complete turns, L2 per
    `batch` L1s — the pyramid as it stands after the newest turn."""
    chunks = {}
    units = [(t["seq"], t["seq"], t["seq"]) for t in turns]
    seq, level = 0, 1
    while len(units) >= batch:
        rows = []
        for i in range(0, len(units) - len(units) % batch, batch):
            seq += 1
            g = units[i:i + batch]
            frm = g[0][0] if level == 1 else g[0][2]
            to = g[-1][1] if level == 1 else g[-1][2]
            rows.append({"seq": seq, "level": level, "fromSeq": frm, "toSeq": to,
                         "summary": f"L{level} #{seq} " + "s" * 200})
        chunks[level] = rows
        units = [(r["fromSeq"], r["toSeq"], r["seq"]) for r in rows]
        level += 1
    return chunks


def test_300_turns_each_window_is_a_byte_prefix_of_the_next_except_at_jumps():
    # ADR-006 §2: between anchor jumps run N+1's window extends run N's
    # byte for byte (the cache read at the boot end); a jump, or a new
    # L2 chunk, are the only rewrites — and there are few of them
    import random
    rnd = random.Random(300)
    turns = [_sized(i, rnd.randint(300, 2500)) for i in range(1, 301)]
    budget = 40_000
    high, low = H["water_marks"](budget)
    prev_anchor, prev_msgs, prev_l2 = None, None, 0
    jumps, rewrites = 0, 0
    for n in range(1, 301):
        seen = turns[:n - 1]                    # the turns before this run's message
        chunks = _rollup(seen)
        chunks = {k: v for k, v in chunks.items() if v}
        anchor = H["choose_anchor"](seen, chunks.get(1, []), prev_anchor, high, low)
        msgs = H["render_boot_window"](seen, chunks, total_tokens=budget, anchor=anchor)
        tail_cost = sum(H["approx_tokens"](p["text"]) for m in msgs for p in m["parts"]
                        if not p["text"].startswith("[earlier context"))
        n_l2 = len(chunks.get(2, []))
        if prev_msgs is not None:
            if anchor != prev_anchor:
                jumps += 1
                assert tail_cost <= low                      # a jump lands under low-water
            elif n_l2 != prev_l2:
                rewrites += 1
            else:
                assert tail_cost <= high
                assert msgs[:len(prev_msgs)] == prev_msgs, n   # byte-prefix extension
        prev_anchor, prev_msgs, prev_l2 = anchor, msgs, n_l2
    assert 0 < jumps <= 20, jumps
    assert rewrites <= 3, rewrites


def test_approx_tokens_two_and_a_half_chars_per_token():
    # ADR-006 §2: history is ids, links and code — 2.5 chars/token
    assert H["approx_tokens"]("") == 0
    assert H["approx_tokens"]("ab") == 1
    assert H["approx_tokens"]("abc") == 2
    assert H["approx_tokens"]("abcde") == 2
    assert H["approx_tokens"]("a" * 250) == 100


def test_approx_tokens_within_15pct_of_count_tokens_on_history_like_text():
    # a synthetic history-like conversation (prose, bafy ids, any://
    # links, code, stamps, footers), token-counted once with the
    # count_tokens API (claude-sonnet-5, 2026-10-16) — the estimator
    # must land within ±15% of the real count
    fx = json.loads((Path(__file__).parent / "fixtures"
                     / "boot-window-calibration.json").read_text())
    est = sum(H["approx_tokens"](m["content"]) for m in fx["messages"])
    assert fx["chars"] == sum(len(m["content"]) for m in fx["messages"])
    assert abs(est - fx["tokens"]) / fx["tokens"] < 0.15, (est, fx["tokens"])


def _tree(n_turns, batch=10):
    """A rollup pyramid over n_turns: L1 per `batch` turns, L2 per
    `batch` L1s, … — contiguous, non-overlapping, like rollup@v1."""
    chunks = {}
    seq = 0
    units = [(i, i) for i in range(1, n_turns + 1)]     # (fromSeq, toSeq) in turns
    level = 1
    while len(units) >= batch:
        rows = []
        for i in range(0, len(units) - len(units) % batch, batch):
            seq += 1
            group = units[i:i + batch]
            # level-1 ranges are turn seqs; higher levels range over child chunk seqs
            frm = group[0][0] if level == 1 else group[0][2]
            to = group[-1][1] if level == 1 else group[-1][2]
            rows.append({"seq": seq, "level": level, "fromSeq": frm, "toSeq": to,
                         "summary": f"L{level} #{seq}"})
        chunks[level] = rows
        units = [(r["fromSeq"], r["toSeq"], r["seq"]) for r in rows]
        level += 1
    return chunks


def _turn_cover(chunk, chunks):
    """The turn seqs a chunk covers (recursively through its children)."""
    if chunk["level"] == 1:
        return set(range(chunk["fromSeq"], chunk["toSeq"] + 1))
    out = set()
    for c in chunks[chunk["level"] - 1]:
        if chunk["fromSeq"] <= c["seq"] <= chunk["toSeq"]:
            out |= _turn_cover(c, chunks)
    return out


def test_no_turn_is_covered_twice_across_levels_and_the_raw_tail():
    # property: over random pyramids, raw tails and budgets, every turn
    # seq appears at most once — raw, or in exactly one chunk line
    import random
    rnd = random.Random(20261016)
    for _ in range(150):
        n = rnd.randint(1, 400)
        chunks = _tree(n, batch=rnd.choice((3, 5, 10)))
        raw_from = rnd.randint(max(1, n - 30), n)
        turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(raw_from, n + 1)]
        msgs = H["render_boot_window"](turns, chunks,
                                       total_tokens=rnd.choice((200, 1000, 5000, 40000)))
        seen = {}
        for m in msgs:   # the raw turns the window actually holds
            text = m["parts"][0]["text"]
            if m["role"] == "user" and text.startswith("q"):
                seen[int(text[1:])] = "raw"
        if msgs and msgs[0]["parts"][0]["text"].startswith("[earlier context"):
            for line in msgs[0]["parts"][0]["text"].splitlines()[1:]:
                seq = int(line.split("[chunk #")[1].split(" ")[0])
                level = int(line.split("(L")[1].split(")")[0])
                chunk = next(c for c in chunks[level] if c["seq"] == seq)
                for s in _turn_cover(chunk, chunks):
                    assert s not in seen, (n, raw_from, seq, level, s, seen[s])
                    seen[s] = f"L{level}#{seq}"


def test_a_partial_lower_cover_is_replaced_by_its_parent_chunk():
    # L1 #1–10 over turns 1–100, L2 #11 over L1 #1–10; the budget fits
    # only the newest few L1 lines → the L2 replaces them whole (older
    # material degrades as a unit; nothing is listed twice)
    chunks = _tree(100)
    turns = [_turn(101, "q", ["a"])]
    line = H["_chunk_line"](chunks[1][0])
    budget = H["approx_tokens"](line) * 3 + 2 + H["approx_tokens"]("qa")
    msgs = H["render_boot_window"](turns, chunks, total_tokens=budget)
    body = msgs[0]["parts"][0]["text"]
    assert "L2 #11" in body and "L1 #" not in body


def test_a_parent_chunk_is_skipped_when_all_its_children_fit():
    chunks = _tree(100)
    turns = [_turn(101, "q", ["a"])]
    msgs = H["render_boot_window"](turns, chunks, total_tokens=40_000)
    body = msgs[0]["parts"][0]["text"]
    assert all(f"L1 #{i}" in body for i in range(1, 11)) and "L2 #11" not in body


def test_a_parent_chunk_overlapping_the_raw_tail_is_never_rendered():
    # the raw tail starts inside L1 #10 (turns 91–100): neither L1 #10
    # nor its parent L2 #11 may summarize what is present raw
    chunks = _tree(100)
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(95, 101)]
    line = H["_chunk_line"](chunks[1][0])
    raw_cost = sum(H["approx_tokens"](p["text"]) for t in turns
                   for m in H["_turn_messages"](t) for p in m["parts"])
    budget = H["approx_tokens"](line) * 2 + 2 + raw_cost
    msgs = H["render_boot_window"](turns, chunks, total_tokens=budget)
    body = msgs[0]["parts"][0]["text"]
    assert "L1 #10" not in body and "L2 #11" not in body


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


def test_a_past_turn_keeps_its_time_view_line_and_effect_summary():
    # the next run lost where the user was and what the turn did: a
    # no-view follow-up then "retracted" a correct view-based answer
    turn = {"seq": 1, "userText": "What's this page about?",
            "replies": ["It's Dune (Book, 1965)."],
            "context": "[now: Tue 2026-09-29 13:23 +02:00 | user's view — space: sp1, object: o1]",
            "effects": ["any.query_objects ×2", "any.get_markdown ×1"],
            "traceRef": "run_abc"}
    msgs = H["render_boot_window"]([turn], {}, total_tokens=10_000)
    assert [m["role"] for m in msgs] == ["user", "assistant"]   # roles still alternate
    user = msgs[0]["parts"][0]["text"]
    assert user.startswith("What's this page about?")
    assert "[Tue 2026-09-29 13:23 +02:00 | user's view — space: sp1, object: o1]" in user
    assert "[ran: any.query_objects×2, any.get_markdown×1 · run_abc]" in user
    assert msgs[1]["parts"][0]["text"] == "It's Dune (Book, 1965)."   # reply untouched
    # an old turn without the new fields renders as before
    old = H["render_boot_window"]([_turn(2, "q", ["a"])], {}, total_tokens=10_000)
    assert old[0]["parts"][0]["text"] == "q"


def _ctx(day, time, view="no view: currentUserSpace is None"):
    return f"[now: {day} {time} +02:00 | {view}]"


def test_stamps_go_compact_after_the_first_turn_until_the_day_or_view_changes():
    # ADR-005 §5: full stamp first; [HH:MM] while the day and the view
    # hold; the view again when it changes; the date again on a new day
    v1 = "user's view — space: sp1"
    def turn(seq, text, ctx):
        return {"seq": seq, "userText": text, "replies": ["r"], "context": ctx}
    turns = [
        turn(1, "a", _ctx("Thu 2026-10-15", "18:10", v1)),
        turn(2, "b", _ctx("Thu 2026-10-15", "18:12", v1)),
        turn(3, "c", _ctx("Thu 2026-10-15", "18:30")),
        turn(4, "d", _ctx("Fri 2026-10-16", "09:00")),
        turn(5, "e", "[something else]"),
    ]
    msgs = H["render_boot_window"](turns, {}, total_tokens=10_000)
    users = [m["parts"][0]["text"] for m in msgs if m["role"] == "user"]
    assert users[0] == "a\n\n[Thu 2026-10-15 18:10 +02:00 | user's view — space: sp1]"
    assert users[1] == "b\n\n[18:12]"
    assert users[2] == "c\n\n[18:30 | no view: currentUserSpace is None]"
    assert users[3] == "d\n\n[Fri 2026-10-16 09:00 +02:00]"
    assert users[4] == "e\n\n[something else]"           # unparseable: verbatim
    # the compact form is a real saving on the stamps alone
    full = sum(len(t["context"]) for t in turns[:4])
    compact = sum(len(u.split("\n\n", 1)[1]) for u in users[:4])
    assert compact < full * 0.6


def test_effects_footer_is_compact_and_drops_clock_and_trace_reads():
    turn = {"seq": 1, "userText": "q", "replies": ["r"], "traceRef": "run_x",
            "effects": ["time.now ×46", "any.query_objects ×3", "trace.effects_of ×2",
                        "any.upsert_record ×2"]}
    def user_text(t):
        return H["render_boot_window"]([t], {}, total_tokens=10_000)[0]["parts"][0]["text"]
    assert user_text(turn) == "q\n\n[ran: any.query_objects×3, any.upsert_record×2 · run_x]"
    # noise only → no footer at all; no trace ref → no ref
    assert user_text({**turn, "effects": ["time.now ×1"]}) == "q"
    no_ref = {**turn, "traceRef": ""}
    assert "[ran: any.query_objects×3, any.upsert_record×2]" in user_text(no_ref)
