"""toolcaller@v1 — the loop as a guest program, exec'd with fake
globals: scripted llm module, fake any client, fake subcell, recorded
mailbox/span/trace effects."""

from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "programs" / "toolcaller@v1.py").read_text()


def tool_reply(code="1+1", cid="cell_x", usage=None):
    return {"parts": [{"type": "tool_call", "id": cid, "name": "run_cell",
                       "args": {"code": code}}],
            "stop": "tool", "usage": usage or {"in": 10, "out": 5}}


def done_reply(text="done", usage=None):
    return {"parts": [{"type": "text", "text": text}], "stop": "done",
            "usage": usage or {"in": 5, "out": 2}}


class World:
    """Every seam the toolcaller touches, recorded."""

    def __init__(self, replies, cells=None, mailbox=None, hits=None,
                 ui_ctx=None):
        self.replies = list(replies)
        self.cells = list(cells or [])
        self.mail = list(mailbox or [])
        self.llm_calls = []
        self.chat_posts = []
        self.turns = []
        self.roi = []
        self.spans = []
        self.plan_hits = hits or {"messages": [], "injected": []}
        self.ui_ctx = ui_ctx

    # --- guest globals -----------------------------------------------------
    def effect(self, name, payload=None):
        if name == "mailbox.drain":
            items, self.mail = self.mail, []
            return {"items": items}
        if name == "span.begin":
            self.spans.append(("begin", payload["name"]))
            return {"span": f"s{len(self.spans)}"}
        if name == "span.end":
            self.spans.append(("end", payload["ok"]))
            return None
        if name == "trace.effects_of":
            return {"records": [{"seq": 9, "effect": "http.post",
                                 "class": "mutate", "mocked": False, "error": None}]}
        raise AssertionError(f"unexpected effect {name}")

    def subcell(self, code, cell_id):
        return self.cells.pop(0) if self.cells else {
            "ok": True, "prints": [], "last": {"repr": "2", "size": 1, "schema": "int"},
            "error": None}

    def use(self, spec):
        w = self

        class Client:
            def chat_send(self, space, chat, body):
                w.chat_posts.append(body)
                return {"recordIds": ["m1"]}

            def append_turn(self, space, chat, body):
                w.turns.append(body)
                return {"seq": len(w.turns) - 1}

            def get_ui_context(self, space):
                return w.ui_ctx

            # compose_system reads the space (skills/tools/brain);
            # an empty space composes an empty prompt prefix
            def list_types(self, space):
                return []

            def query_objects(self, space, filter=None, **kw):
                return []

            def query(self, space, oid, dataset, **kw):
                return []

            def get_brain(self, space):
                return {}

        class Llm:
            @staticmethod
            def chat(messages, system="", tier="codegen", tools=None):
                w.llm_calls.append({"messages": [dict(m) for m in messages],
                                    "system": system, "tier": tier, "tools": tools})
                return w.replies.pop(0)

        class Hist:
            @staticmethod
            def recent_turns(c, s, ch, n):
                return []

            @staticmethod
            def chunks_at_level(c, s, ch, lvl, n):
                return []

            @staticmethod
            def render_boot_window(turns, chunks, total_tokens=40000):
                return [{"role": "user", "parts": [{"type": "text",
                                                    "text": "[earlier context]"}]}] \
                    if turns else []

            @staticmethod
            def raw_tail(turns, total_tokens=40000):
                return turns

        class Ar:
            @staticmethod
            def plan(c, s, q, boot_min_seq, policy=None):
                return w.plan_hits

            @staticmethod
            def log_roi(c, s, injected, replies, ts):
                w.roi.append((injected, replies))

        return {"any@v1": type("M", (), {"client": staticmethod(lambda: Client())}),
                "llm@v1": Llm, "history@v1": Hist, "autorecall@v1": Ar}[spec]


def run(world, **args):
    g = {"effect": world.effect, "use": world.use, "subcell": world.subcell,
         "now": lambda: 1234}
    exec(compile(SRC, "toolcaller@v1.py", "exec"), g)
    return g["main"]({"space": "s1", "chatId": "c1", "userText": "go",
                      "traceRef": "run_x", **args})


def test_done_turn_posts_reply_and_persists_turn():
    w = World([done_reply("hello")])
    out = run(w)
    assert out["stop"] == "done" and out["replies"] == ["hello"]
    assert w.chat_posts[-1] == {"text": "hello",
                                "agent": {"name": "bao", "done": True}}
    turn = w.turns[0]
    assert turn["userText"] == "go" and turn["traceRef"] == "run_x"
    assert turn["llm"]["stopReason"] == "done"


def test_cell_turn_spans_digest_and_tool_result():
    cell = {"ok": True,
            "prints": [{"repr": "42", "size": 2, "schema": "int"}],
            "last": {"repr": "x" * 9000, "size": 9000, "schema": "str"},
            "error": None}
    w = World([tool_reply(), done_reply("ok")], cells=[cell])
    out = run(w)
    assert out["turns"] == 2
    assert ("begin", "cell") in w.spans and ("end", True) in w.spans
    result_msg = w.llm_calls[1]["messages"][-1]
    part = result_msg["parts"][0]
    assert part["type"] == "tool_result" and part["call_id"] == "cell_x"
    assert "#0 42" in part["content"]
    assert 'values.get("cell_x", \'last\')' in part["content"]   # stub over budget
    assert "mutate http.post #9" in part["content"]              # side effects


def test_cell_error_marks_tool_result_is_error():
    cell = {"ok": False, "prints": [], "last": None,
            "error": {"type": "ValueError", "message": "boom", "traceback": "tb"}}
    w = World([tool_reply(), done_reply("ok")], cells=[cell])
    run(w)
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["is_error"] is True and "ValueError: boom" in part["content"]
    assert ("end", False) in w.spans


def test_turn_ceiling_wraps_up_not_cuts():
    w = World([tool_reply(), done_reply("summary")],
              cells=[{"ok": True, "prints": [], "last": None, "error": None}])
    out = run(w, maxTurns=1)
    assert out["stop"] == "wrapup" and out["replies"] == ["summary"]
    wrap_msg = w.llm_calls[-1]["messages"][-1]
    assert "turn ceiling" in wrap_msg["parts"][0]["text"]
    assert w.llm_calls[-1]["tools"] == []                        # no more cells


def test_mailbox_inject_and_soft_break_are_drained_effects():
    w = World([done_reply("wrapped")],
              mailbox=[{"kind": "inject", "text": "also X"}, {"kind": "break"}])
    out = run(w)
    assert out["stop"] == "wrapup"
    first = w.llm_calls[0]["messages"]
    assert any(p.get("text") == "also X"
               for m in first for p in m["parts"] if p["type"] == "text")


def test_boot_window_and_injection_bracket_the_user_message():
    w = World([done_reply("ok")])
    w.plan_hits = {"messages": [{"role": "assistant", "parts": [
        {"type": "tool_call", "id": "a0", "name": "recall", "args": {}}]},
        {"role": "user", "parts": [{"type": "tool_result", "call_id": "a0",
                                    "content": "Memories:", "is_error": False}]}],
        "injected": [({"objectId": "b"}, {"id": "m1"})]}

    class WithHistory(World):
        def use(self, spec):
            mod = super().use(spec)
            if spec == "history@v1":
                mod.recent_turns = staticmethod(
                    lambda c, s, ch, n: [{"seq": 1, "userText": "old",
                                          "replies": ["r"]}])
            return mod

    w.__class__ = type("W2", (WithHistory,), {})
    out = run(w)
    msgs = w.llm_calls[0]["messages"]
    assert msgs[0]["parts"][0]["text"] == "[earlier context]"
    assert msgs[1]["parts"][0]["text"].startswith("go\n\n[now: ")
    assert msgs[2]["parts"][0]["type"] == "tool_call"
    # ROI logged with the final replies
    assert w.roi and w.roi[0][1] == ["ok"]
    assert out["injected"] == 1


def test_progress_bubbles_for_interim_text():
    reply = tool_reply()
    reply["parts"].insert(0, {"type": "text", "text": "working on it"})
    w = World([reply, done_reply("done")],
              cells=[{"ok": True, "prints": [], "last": None, "error": None}])
    run(w)
    assert {"text": "working on it",
            "agent": {"name": "bao", "done": False}} in w.chat_posts


def test_unknown_stop_reason_raises():
    w = World([{"parts": [], "stop": "weird", "usage": {}}])
    with pytest.raises(RuntimeError, match="unhandled stop reason"):
        run(w)


# --- quiet mode (ADR-008 §5) -------------------------------------------------


def test_quiet_mode_isolates_the_run():
    w = World([done_reply("report")], mailbox=[{"kind": "break"}])
    out = run(w, quiet=True)
    assert out["stop"] == "done" and out["replies"] == ["report"]
    # no bubbles, no persisted turn, no ROI
    assert w.chat_posts == [] and w.turns == [] and w.roi == []
    # the parent's mailbox is NOT consumed
    assert w.mail == [{"kind": "break"}]
    # fresh context: exactly the task text, no boot window / recall plan
    msgs = w.llm_calls[0]["messages"]
    assert len(msgs) == 1 and msgs[0]["parts"][0]["text"].startswith("go")
    assert "## Subagent" in w.llm_calls[0]["system"]


def test_quiet_mode_ceiling_still_wraps_up():
    w = World([tool_reply(), done_reply("summary")],
              cells=[{"ok": True, "prints": [], "last": None, "error": None}])
    out = run(w, quiet=True, maxTurns=1)
    assert out["stop"] == "wrapup" and out["replies"] == ["summary"]
    assert w.chat_posts == [] and w.turns == []


# --- runtime context + ui-context suffix (ADR-005 §5) ---------------------------


def test_user_message_carries_timestamp_and_view_context():
    w = World([done_reply()],
              ui_ctx={"spaceId": "sp9", "objectId": "ob3",
                      "view": "object", "updatedAt": 1_200_000})
    run(w)
    call = w.llm_calls[0]
    user = call["messages"][-1]["parts"][0]["text"]
    assert user.startswith("go\n\n[now: ")
    assert "user's view — space: sp9, object: ob3, view: object" in user
    assert "34s ago" in user  # now()=1234s, pointer at 1200s
    # runtime context is appended to the system arg, guest-side
    assert "## Runtime context" in call["system"]
    assert "`s1`" in call["system"] and "`c1`" in call["system"]
    # the persisted turn keeps the raw userText — suffix is llm-only
    assert w.turns[0]["userText"] == "go"


def test_context_suffix_degrades_to_timestamp_without_pointer():
    w = World([done_reply()])  # ui_ctx None
    run(w)
    user = w.llm_calls[0]["messages"][-1]["parts"][0]["text"]
    assert "[now: " in user and "user's view" not in user
