"""Trace viewer (ADR-006 § traceRef) — pure rendering over a fabricated
ADR-001 trace, fully offline."""

import pytest
from anybao.viewer import build_view, render_trace, turn_anchor
from anyrt import trace as tr


def llm_output(parts, stop="tool"):
    return {"parts": parts, "stop": stop, "usage": {"in": 10, "out": 5}}


def make_trace() -> tr.TraceWriter:
    """header + turn 1 (tool_call -> cell + attributed effects) +
    turn 2 (plain text done) + one non-cell effect."""
    w = tr.TraceWriter(run={"id": "run_1", "program": "demo@v1"})
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [], "tier": "codegen"}, key="sha256:aa",
        output=llm_output([
            {"type": "text", "text": "Let me check."},
            {"type": "tool_call", "id": "toolu_abc", "name": "run_cell",
             "args": {"code": 'print("hi")\n1 + 1'}},
        ]),
        meta={"durMs": 320, "class": "read", "usage": {"in": 10, "out": 5}},
    )
    w.effect(
        effect="fetch", cell="toolu_abc",
        input={"url": "http://x"}, key="sha256:bb",
        output={"status": 200}, meta={"durMs": 88, "class": "read"},
    )
    w.effect(
        effect="chat.send", cell="toolu_abc",
        input={"text": "hi"}, key="sha256:cc",
        output=None, meta={"durMs": 3, "class": "mutate"},
    )
    w.cell(
        cell="toolu_abc", ok=True,
        metrics={"fuel_used": 184223, "mem_pages": 512, "duration_ms": 240},
    )
    w.effect(
        effect="time.now", cell=None,
        input={}, key="sha256:dd", output=1234.5, meta={"class": "read"},
    )
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [], "tier": "codegen"}, key="sha256:ee",
        output=llm_output([{"type": "text", "text": "All done."}], stop="done"),
        meta={"usage": {"in": 20, "out": 7}},
    )
    return w


def test_render_trace_timeline():
    w = make_trace()
    out = render_trace(w.records, w.blobs)

    assert "run run_1 — demo@v1" in out
    assert "turn 1 (seq 1)" in out
    assert "turn 2 (seq 6)" in out
    assert "assistant: Let me check." in out
    assert "assistant: All done." in out
    # cell: code, attributed effects, metrics from the cell record
    assert "cell toolu_abc:" in out
    assert 'print("hi")' in out
    assert "1 + 1" in out
    assert "#2 fetch [read, 88ms]" in out
    assert "#3 chat.send [mutate, 3ms]" in out
    assert "result: ok  fuel=184223 mem_pages=512 duration=240ms" in out
    # non-cell effect grouped under the turn, not the cell
    assert "#5 time.now" in out
    assert "totals: 2 turns, 5 effects, tokens in=30 out=12" in out


def test_render_orders_turns_and_cells():
    w = make_trace()
    out = render_trace(w.records)
    assert out.index("turn 1") < out.index("cell toolu_abc:") < out.index("turn 2")


def test_view_attributes_effects_to_cells():
    w = make_trace()
    view = build_view(w.records)
    assert [t.n for t in view.turns] == [1, 2]
    (run,) = view.turns[0].cells
    assert run.cell_id == "toolu_abc"
    assert [e["effect"] for e in run.effects] == ["fetch", "chat.send"]
    assert run.end is not None and run.end["ok"] is True
    assert [e["effect"] for e in view.turns[0].effects] == ["time.now"]
    assert view.turns[1].cells == []


def test_effect_error_and_failed_cell_render():
    w = tr.TraceWriter(run={"id": "run_2"})
    w.effect(
        effect="llm.chat", cell=None, input={}, key="sha256:aa",
        output=llm_output([
            {"type": "tool_call", "id": "cell_x", "name": "run_cell",
             "args": {"code": "boom()"}},
        ]),
    )
    w.effect(
        effect="fetch", cell="cell_x", input={}, key="sha256:bb",
        error={"type": "HTTPError", "message": "503"},
    )
    w.cell(cell="cell_x", ok=False, error={"type": "NameError", "message": "boom"})
    out = render_trace(w.records)
    assert "!! HTTPError: 503" in out
    assert "result: error NameError: boom" in out


def test_blob_refs_render_as_byte_notes():
    w = tr.TraceWriter(run={"id": "run_3"}, blob_threshold=16)
    w.effect(
        effect="llm.chat", cell=None, input={}, key="sha256:aa",
        output=llm_output([{"type": "text", "text": "spilled turn " + "x" * 64}], stop="done"),
    )
    w.effect(
        effect="fetch", cell=None, input={}, key="sha256:bb",
        output={"body": "y" * 64},
    )
    rec = w.records[2]
    assert rec["output"]["__blob"] in w.blobs

    out = render_trace(w.records, w.blobs)
    # llm.chat output resolved via blobs so the turn text still renders
    assert "assistant: spilled turn" in out
    # a generic effect blob is never inlined — byte note only
    assert f"[{rec['output']['bytes']} bytes]" in out
    assert "y" * 64 not in out

    # without the sidecar the turn degrades to a note instead of raising
    out2 = render_trace(w.records)
    assert "[output spilled," in out2


def test_turn_anchor_resolves_nth_llm_chat():
    w = make_trace()
    assert turn_anchor(w.records, 1)["seq"] == 1
    assert turn_anchor(w.records, 2)["seq"] == 6
    with pytest.raises(IndexError):
        turn_anchor(w.records, 3)
    with pytest.raises(IndexError):
        turn_anchor(w.records, 0)
