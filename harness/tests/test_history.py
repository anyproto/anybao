from anybao.history import BootWindowPolicy, build_turn, render_boot_window
from anybao.loop import Outcome


def outcome(replies, stop="done"):
    return Outcome(replies=replies, stop=stop, turns=1, tokens=100)


def test_build_turn_replies_vs_think_and_neutral_stop():
    t = build_turn(user_text="hi", outcome=outcome(["hello"]),
                   think="user greets", trace_ref="run1")
    assert t["userText"] == "hi"
    assert t["replies"] == ["hello"]        # what the user saw
    assert t["think"] == "user greets"      # narration NOT in chat (distinct)
    assert t["traceRef"] == "run1"
    assert t["llm"]["stopReason"] == "done"  # neutral outcome, not raw provider
    assert t["interrupted"] is False
    assert "seq" not in t                    # server-assigned


def test_build_turn_interrupted_on_hard_break():
    t = build_turn(user_text="x", outcome=outcome(["stopped"], stop="break_hard"))
    assert t["interrupted"] is True
    assert t["llm"]["stopReason"] == "break_hard"


def _turn(seq, ut, replies):
    return {"seq": seq, "userText": ut, "replies": replies}


def _chunk(seq, level, frm, to, summary):
    return {"seq": seq, "level": level, "fromSeq": frm, "toSeq": to, "summary": summary}


def test_boot_window_newest_raw_turns_included():
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(10)]
    msgs = render_boot_window(raw_turns=turns, chunks_by_level={},
                              policy=BootWindowPolicy(total_tokens=10_000))
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
    msgs = render_boot_window(raw_turns=turns, chunks_by_level=chunks,
                              policy=BootWindowPolicy(total_tokens=4000))
    joined = " ".join(p["text"] for m in msgs for p in m["parts"])
    assert "earlier context, compressed" in joined
    assert "chunk #" in joined                       # drill-down handles present
    assert "L1 sum 8" in joined                       # newest L1 chunk


def test_boot_window_budget_respected():
    turns = [_turn(i, "q" * 200, ["a" * 200]) for i in range(50)]
    chunks = {1: [_chunk(s, 1, s, s, "s" * 100) for s in range(50)]}
    msgs = render_boot_window(raw_turns=turns, chunks_by_level=chunks,
                              policy=BootWindowPolicy(total_tokens=1000))
    total = sum(len(p["text"]) for m in msgs for p in m["parts"])
    # ~4 chars/token budget of 1000 → well under 1000*4*1.5 slack
    assert total < 1000 * 4 * 2


def test_boot_window_skips_double_covered_l1_chunk():
    # raw tail covers seq 5..9; an L1 chunk over 5..9 must NOT also appear
    turns = [_turn(i, f"q{i}", [f"a{i}"]) for i in range(5, 10)]
    chunks = {1: [_chunk(0, 1, 0, 4, "old range"), _chunk(1, 1, 5, 9, "covered range")]}
    msgs = render_boot_window(raw_turns=turns, chunks_by_level=chunks,
                              policy=BootWindowPolicy(total_tokens=10_000))
    joined = " ".join(p["text"] for m in msgs for p in m["parts"])
    assert "old range" in joined            # older chunk kept
    assert "covered range" not in joined    # double-covered chunk skipped
