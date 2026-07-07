"""Loop ceilings + mailbox (ADR-005 §3) — pure logic, FakeExecutor +
scripted llm, fully offline."""

from anybao.loop import LoopPolicy, Mailbox, run_conversation
from anyrt import trace as tr
from anyrt.effects import Broker, Registry, effect
from anyrt.executor import CellResult, FakeExecutor, ValueRef


def scripted_llm(replies):
    reg = Registry()
    q = list(replies)
    seen = []

    @effect("llm.chat", kind="read", registry=reg)
    def llm_chat(ctx, messages, system="", tier="codegen", tools=None):
        seen.append(messages[-1])
        return q.pop(0)

    return reg, seen


def tool_reply(code="1+1", usage=None):
    return {
        "parts": [{"type": "tool_call", "id": "cell_x", "name": "run_cell",
                   "args": {"code": code}}],
        "stop": "tool", "usage": usage or {"in": 10, "out": 5},
    }


def done_reply(text="done", usage=None):
    return {"parts": [{"type": "text", "text": text}], "stop": "done",
            "usage": usage or {"in": 5, "out": 2}}


def ok_cell():
    return CellResult(cell_id="cell_x", ok=True, last_value=ValueRef("2", 1, "int"))


def run(replies, cells, **kw):
    reg, seen = scripted_llm(replies)
    broker = Broker(reg, tr.TraceWriter(run={"id": "t"}))
    fake = FakeExecutor(cells)
    return run_conversation("go", broker=broker, executor=fake, **kw), seen, fake


def test_normal_done():
    outcome, _, _ = run([done_reply("hello")], [])
    assert outcome.stop == "done" and outcome.replies == ["hello"]


def test_token_ceiling_triggers_wrapup_not_cut():
    # tool reply burns tokens; next loop sees ceiling -> wrap-up call
    replies = [tool_reply(usage={"in": 900, "out": 200}), done_reply("summary")]
    outcome, seen, _ = run(replies, [ok_cell()],
                           policy=LoopPolicy(max_tokens_total=1000))
    assert outcome.stop == "wrapup" and outcome.replies == ["summary"]
    # the wrap-up user message names the reason
    assert any("ceiling" in p["text"]
               for m in seen if m["role"] == "user"
               for p in m["parts"] if p.get("type") == "text")


def test_turn_ceiling_triggers_wrapup():
    replies = [tool_reply(), tool_reply(), done_reply("s")]
    outcome, _, _ = run(replies, [ok_cell(), ok_cell()],
                        policy=LoopPolicy(max_turns=1))
    # turn 1 runs a cell; turn 2 blocked by ceiling -> wrapup uses reply[1]
    assert outcome.stop == "wrapup"


def test_mailbox_inject_appends_user_message():
    mb = Mailbox()
    mb.inject("also check X")
    replies = [done_reply("ok")]
    outcome, seen, _ = run(replies, [], mailbox=mb)
    assert outcome.stop == "done"
    first = seen[0]
    assert first["parts"][-1]["text"] == "also check X"


def test_mailbox_soft_break_wraps_up():
    mb = Mailbox()
    mb.break_soft()
    outcome, _, _ = run([done_reply("wrapped")], [], mailbox=mb)
    assert outcome.stop == "wrapup" and outcome.replies == ["wrapped"]


def test_mailbox_hard_break_interrupts():
    mb = Mailbox()
    mb.break_hard()
    outcome, _, fake = run([done_reply("never")], [], mailbox=mb)
    assert outcome.stop == "break_hard"
    assert fake.interrupted  # executor.interrupt() called


def test_length_stop_wraps_up():
    outcome, _, _ = run(
        [{"parts": [{"type": "text", "text": "partial"}], "stop": "length",
          "usage": {"in": 5, "out": 5}}, done_reply("wrap")], [])
    assert outcome.stop == "wrapup" and outcome.replies == ["wrap"]
