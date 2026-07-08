"""Trace viewer (ADR-006 § traceRef) — pure rendering over a fabricated
ADR-001 trace, fully offline."""

import pytest
from anybao.viewer import (
    build_view,
    render_run,
    render_run_summary,
    render_trace,
    turn_anchor,
)
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


# ---- render_run / render_run_summary (M6 trace viewer proper) ---------------

DIGEST = "Last value: 3\n\nSide effects: any.search ×1"


def make_convo_trace() -> tr.TraceWriter:
    """Conversation-shaped: llm.chat inputs carry the growing messages
    array (boot window, user text, tool_result digests) so the run
    render can recover user text and per-cell digests from deltas."""
    w = tr.TraceWriter(run={"id": "run_c", "program": "toolcaller", "chatId": "chat9"})
    boot = {"role": "user", "parts": [{"type": "text", "text": "old history line"}]}
    user = {"role": "user", "parts": [{"type": "text", "text": "count the notes"}]}
    parts1 = [
        {"type": "text", "text": "On it."},
        {"type": "tool_call", "id": "cell_1", "name": "run_cell",
         "args": {"code": "n = 3\nn"}},
    ]
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [boot, user], "tier": "codegen"}, key="sha256:t1",
        output=llm_output(parts1),
        meta={"durMs": 300, "class": "read",
              "usage": {"in": 100, "out": 9, "costUsd": 0.0123}},
    )
    w.effect(
        effect="any.search", cell="cell_1",
        input={"q": "notes"}, key="sha256:t2",
        output={"hits": 3}, meta={"durMs": 12, "class": "read"},
    )
    w.effect(
        effect="chat.send", cell="cell_1",
        input={"text": "found"}, key="sha256:t3",
        output=None, meta={"durMs": 2, "class": "mutate"},
    )
    w.cell(cell="cell_1", ok=True, metrics={"fuel_used": 5, "duration_ms": 7})
    asst = {"role": "assistant", "parts": parts1}
    result = {"role": "user", "parts": [
        {"type": "tool_result", "call_id": "cell_1", "content": DIGEST},
    ]}
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [boot, user, asst, result], "tier": "codegen"}, key="sha256:t4",
        output=llm_output([{"type": "text", "text": "There are 3 notes."}], stop="done"),
        meta={"usage": {"in": 150, "out": 12}},
    )
    return w


def test_render_run_header_and_totals():
    out = render_run(make_convo_trace().records)
    assert out.startswith("run run_c — toolcaller  chat=chat9\n")
    assert "totals: 2 turns, 1 cells, 4 effects (1 mutate), tokens in=250 out=21" in out


def test_render_run_turn_blocks():
    out = render_run(make_convo_trace().records)
    assert "#turn_1 (seq 1)" in out and "#turn_2 (seq 5)" in out
    # user text = the LAST text-bearing user message of turn 1's input
    assert "user: count the notes" in out
    assert "user: old history line" not in out
    assert out.count("user:") == 1                 # turn 2's delta has no user text
    assert "assistant: On it." in out
    assert "tool_call run_cell (cell_1)" in out
    assert "assistant: There are 3 notes." in out


def test_render_run_marks_mutations():
    out = render_run(make_convo_trace().records)
    assert "* #3 chat.send [mutate, 2ms]" in out   # highlighted
    assert "* #2 any.search" not in out            # reads unmarked
    assert "#2 any.search [read, 12ms]" in out


def test_render_run_cell_digest_and_llm_line():
    out = render_run(make_convo_trace().records)
    assert "result: ok  fuel=5 duration=7ms" in out
    assert "digest:" in out
    assert "Last value: 3" in out                  # the digest the model saw
    assert "llm: tokens in=100 out=9 cost=$0.0123 (300ms)" in out
    assert "llm: tokens in=150 out=12" in out


def test_render_run_degrades_on_spilled_input_without_sidecar():
    w = tr.TraceWriter(run={"id": "run_s"}, blob_threshold=32)
    user = {"role": "user", "parts": [{"type": "text", "text": "x" * 64}]}
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [user], "tier": "codegen"}, key="sha256:s1",
        output=llm_output([{"type": "text", "text": "done"}], stop="done"),
    )
    assert set(w.records[1]["input"]) == {"__blob", "bytes"}
    out = render_run(w.records)                    # no blobs passed — no raise
    assert "user:" not in out
    out2 = render_run(w.records, w.blobs)          # with sidecar the text is back
    assert "user: " + "x" * 64 in out2


def test_render_run_summary_one_liner():
    line = render_run_summary(make_convo_trace().records)
    assert "\n" not in line
    assert line == (
        "run run_c — toolcaller  chat=chat9: 2 turns, 1 cells, "
        "4 effects (1 mutate), tokens in=250 out=21 — ok"
    )


def test_render_run_summary_flags_errors():
    w = tr.TraceWriter(run={"id": "run_e", "program": "p@v1"})
    w.effect(
        effect="llm.chat", cell=None, input={}, key="sha256:aa",
        output=llm_output([
            {"type": "tool_call", "id": "cell_x", "name": "run_cell",
             "args": {"code": "boom()"}},
        ]),
    )
    w.cell(cell="cell_x", ok=False, error={"type": "NameError", "message": "boom"})
    assert render_run_summary(w.records).endswith("— error")


def test_turn_anchor_resolves_nth_llm_chat():
    w = make_trace()
    assert turn_anchor(w.records, 1)["seq"] == 1
    assert turn_anchor(w.records, 2)["seq"] == 6
    with pytest.raises(IndexError):
        turn_anchor(w.records, 3)
    with pytest.raises(IndexError):
        turn_anchor(w.records, 0)


# ---- spans render collapsed by default (ADR-001 §4c) -------------------------

def make_span_trace() -> tr.TraceWriter:
    """One turn, one cell whose facade call wraps a mutate primitive in
    a span; one primitive outside the span."""
    w = tr.TraceWriter(run={"id": "run_sp", "program": "demo@v1"})
    w.effect(
        effect="llm.chat", cell=None, input={}, key="sha256:aa",
        output=llm_output([
            {"type": "tool_call", "id": "cell_1", "name": "run_cell",
             "args": {"code": "linear.createTask('t')"}},
        ]),
    )
    w.span_begin(span="s1", name="linear.createTask", cell="cell_1",
                 input={"args": ["t"]}, key="sha256:bb")
    w.effect(
        effect="any.modify", cell="cell_1", input={"n": 1}, key="sha256:cc",
        output={"ok": 1}, meta={"durMs": 40, "class": "mutate"}, span="s1",
    )
    w.span_end(span="s1", name="linear.createTask", cell="cell_1", ok=True,
               output={"id": "T-1"}, meta={"durMs": 55, "effects": 1, "mutations": 1})
    w.effect(
        effect="time.now", cell="cell_1", input={}, key="sha256:dd",
        output=1.5, meta={"durMs": 1, "class": "read"},
    )
    w.cell(cell="cell_1", ok=True, metrics={"fuel_used": 9})
    return w


def test_render_collapses_spans_by_default():
    w = make_span_trace()
    out = render_trace(w.records)
    assert "#2 linear.createTask [span, 1 effect, 55ms] -> " in out
    assert '{"id": "T-1"}' in out
    assert "any.modify" not in out             # hidden under the span
    assert "#5 time.now" in out                # non-span effect still shown


def test_render_expand_spans_shows_inner_records():
    w = make_span_trace()
    out = render_trace(w.records, expand_spans=True)
    assert "#2 linear.createTask [span, 1 effect, 55ms]" in out
    assert "#3 any.modify [mutate, 40ms]" in out
    # inner record indented under its span line
    span_line = next(ln for ln in out.splitlines() if "linear.createTask" in ln)
    inner_line = next(ln for ln in out.splitlines() if "any.modify" in ln)
    assert len(inner_line) - len(inner_line.lstrip()) > len(span_line) - len(span_line.lstrip())


def test_render_run_marks_mutating_span():
    w = make_span_trace()
    out = render_run(w.records)
    assert "* #2 linear.createTask [span, 1 effect, 55ms]" in out
    assert "any.modify" not in out


def test_render_run_span_error_and_unclosed():
    w = tr.TraceWriter(run={"id": "run_spe"})
    w.effect(
        effect="llm.chat", cell=None, input={}, key="sha256:aa",
        output=llm_output([
            {"type": "tool_call", "id": "cell_1", "name": "run_cell",
             "args": {"code": "boom()"}},
        ]),
    )
    w.span_begin(span="s1", name="helper.boom", cell="cell_1",
                 input={}, key="sha256:bb")
    w.span_end(span="s1", name="helper.boom", cell="cell_1", ok=False,
               error={"type": "ValueError", "message": "nope"})
    w.span_begin(span="s2", name="helper.hang", cell="cell_1",
                 input={}, key="sha256:cc")
    w.cell(cell="cell_1", ok=False, error={"type": "Interrupted", "message": "fuel"})
    out = render_trace(w.records)
    assert "helper.boom [span, 0 effects] !! ValueError: nope" in out
    assert "helper.hang [span, 0 effects] (unclosed)" in out
