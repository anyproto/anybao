"""toolcaller@v1 — the loop as a guest program, exec'd with fake
globals: scripted llm module, fake any client, fake subcell, recorded
mailbox/span/trace effects."""

from pathlib import Path

import pytest
from kernelenv import kernel_globals

SRC = (Path(__file__).resolve().parents[1] / "repos" / "_agent"
       / "programs" / "toolcaller@v1.py").read_text()


def tool_reply(code="1+1", cid="cell_x", usage=None):
    return {"parts": [{"type": "tool_call", "id": cid, "name": "run_cell",
                       "args": {"code": code}}],
            "stop": "tool", "usage": usage or {"in": 10, "out": 5}}


def done_reply(text="done", usage=None):
    return {"parts": [{"type": "text", "text": text}], "stop": "done",
            "usage": usage or {"in": 5, "out": 2}}


class EffectError(Exception):
    """The kernel's typed effect failure, as `effect()` raises it."""


class World:
    """Every seam the toolcaller touches, recorded."""

    def __init__(self, replies, cells=None, mailbox=None, hits=None, traits=None):
        self.replies = list(replies)
        self.traits = {"system_role": "native", "tool_mode": "native",
                       "reasoning": "advisory", "thinking": "default", "cache": "auto",
                       "context_window": 128000, "max_output": 8192, "sampling": {},
                       "prompt_style": "full", "instructions_at": "system",
                       "malformed_retries": 2, **(traits or {})}
        self.boot_budgets = []
        self.cells = list(cells or [])
        self.mail = list(mailbox or [])
        self.llm_calls = []
        self.chat_posts = []
        self.turns = []
        self.roi = []
        self.spans = []
        self.span_ends = []
        self.span_inputs = []
        self.preludes = []
        self.plan_hits = hits or {"messages": [], "injected": []}

    # --- guest globals -----------------------------------------------------
    def effect(self, name, payload=None):
        if name == "mailbox.drain":
            items, self.mail = self.mail, []
            return {"items": items}
        if name == "span.begin":
            mock = (payload.get("input") or {}).get("mock")
            if mock is not None and getattr(self, "reject_mock", None):
                # the host rejects the spec: the span never opens
                raise EffectError(f"mock_spec: {self.reject_mock}")
            self.spans.append(("begin", payload["name"]))
            self.span_inputs.append(payload.get("input") or {})
            return {"span": f"s{len(self.spans)}"}
        if name == "span.end":
            self.spans.append(("end", payload["ok"]))
            self.span_ends.append(payload)
            # the end meta comes back; a World may script mockFilter
            f = getattr(self, "mock_filter", None)
            return {"durMs": 1, "effects": 0, "mutations": 0,
                    **({"mockFilter": f} if f is not None else {})}
        if name == "trace.effects_of":
            if getattr(self, "effect_rows", None) is not None:
                return {"records": list(self.effect_rows)}
            return {"records": [{"seq": 9, "effect": "http.post",
                                 "class": "mutate", "mocked": False, "error": None}]}
        raise AssertionError(f"unexpected effect {name}")

    def subcell(self, code, cell_id):
        if cell_id == "_ctx":   # the bound-globals prelude (ADR-010 §8)
            self.preludes.append(code)
            return {"ok": True, "prints": [], "last": None, "error": None}
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

            # compose_system reads the space (skills/tools/brain);
            # an empty space composes an empty prompt prefix
            def list_types(self, space):
                return []

            def query_objects(self, space, filter=None, **kw):
                return []

            def query(self, space, oid, dataset, **kw):
                return []

            def get_brain(self):
                return {}

            def list_apps(self, space):
                # ADR-027 §5: the sidebar as data — one app in the
                # agent space, a hidden one filtered out
                return [{"name": "Wiki", "usecase": "wiki", "bundleId": "system:wiki/v1",
                         "description": "A tree of pages", "hidden": False},
                        {"name": "General", "usecase": "general-chat", "hidden": True}]

        class Llm:
            @staticmethod
            def chat(messages, system="", tier="codegen", tools=None):
                w.llm_calls.append({"messages": [dict(m) for m in messages],
                                    "system": system, "tier": tier, "tools": tools})
                return w.replies.pop(0)

            @staticmethod
            def profile(tier="codegen"):
                return {"profile": "test", "backend": "generic", "model": "m",
                        "traits": dict(w.traits)}

        class Hist:
            @staticmethod
            def recent_turns(c, s, ch, n):
                return []

            @staticmethod
            def chunks_at_level(c, s, ch, lvl, n):
                return []

            @staticmethod
            def render_boot_window(turns, chunks, total_tokens=40000):
                w.boot_budgets.append(total_tokens)
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

        # any@v1 is a flat module now (ADR-010 §8) — the fake is the
        # client-shaped surface itself
        return {"any@v1": Client(),
                "llm@v1": Llm, "history@v1": Hist, "autorecall@v1": Ar}[spec]


def run(world, **args):
    g = {"effect": world.effect, "use": world.use, "subcell": world.subcell,
         "values": getattr(world, "values", None), "EffectError": EffectError,
         **kernel_globals(now=1234)}
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
    # the LLMStats keys + prompt provenance (ADR-005 §5; `llm` is an
    # object-kind dataset field, nested keys are free — ADR-006 §1); no
    # soul in an empty space → no soulFingerprint
    fp = turn["llm"].pop("promptFingerprint")
    assert len(fp) == 16 and int(fp, 16) >= 0
    assert "soulFingerprint" not in turn["llm"]
    assert turn["llm"] == {"stopReason": "done", "inTokens": 5, "outTokens": 2,
                           "cacheRead": 0, "cacheWrite": 0, "cells": 0}


def test_failed_trailing_append_turn_does_not_fail_the_run():
    # ADR-005 §3: the reply is on screen before the log write; a
    # rejected agent_turns append (e.g. a tombstoned seq after the log
    # was wiped) leaves the run ok and names itself in the result
    w = World([done_reply("hello")])
    real_use = w.use

    def use(spec):
        m = real_use(spec)
        if spec == "any@v1":
            def boom(space, chat, body):
                raise RuntimeError("409 log.seq_collision: agent_turns seq 12 rejected")
            m.append_turn = boom
        return m
    w.use = use
    out = run(w)
    assert out["stop"] == "done" and out["replies"] == ["hello"]
    assert out["logError"].startswith("RuntimeError: 409 log.seq_collision")
    assert w.chat_posts[-1]["text"] == "hello"      # the reply landed, nothing else posted
    assert w.turns == []                            # and the turn really was not logged


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
    llm = dict(w.turns[0]["llm"])
    llm.pop("promptFingerprint")
    assert llm == {"stopReason": "done", "cells": 1,
                   "inTokens": 15, "outTokens": 7,
                   "cacheRead": 0, "cacheWrite": 0}


def test_cell_error_marks_tool_result_is_error():
    cell = {"ok": False, "prints": [], "last": None,
            "error": {"type": "ValueError", "message": "boom", "traceback": "tb"}}
    w = World([tool_reply(), done_reply("ok")], cells=[cell])
    run(w)
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["is_error"] is True and "ValueError: boom" in part["content"]
    assert ("end", False) in w.spans
    # the failure rides the cell span-end record itself (ADR-003 §3):
    # type + message, no traceback — a past-run reader sees WHY
    failed = [e for e in w.span_ends if e["ok"] is False]
    assert failed == [{"ok": False, "error": {"type": "ValueError", "message": "boom"}}]


def test_turn_ceiling_wraps_up_not_cuts():
    w = World([tool_reply(), done_reply("summary")],
              cells=[{"ok": True, "prints": [], "last": None, "error": None}])
    out = run(w, maxTurns=1)
    assert out["stop"] == "wrapup" and out["replies"] == ["summary"]
    wrap_msg = w.llm_calls[-1]["messages"][-1]
    assert "turn ceiling" in wrap_msg["parts"][0]["text"]
    # the tool list stays: it is part of the cached prompt prefix
    assert [t["name"] for t in w.llm_calls[-1]["tools"]] == ["run_cell"]


def test_wrapup_that_answers_with_a_tool_call_gets_one_toolless_retry():
    # the wrap-up asks for text; a model that calls a tool anyway is
    # answered (dangling call) and asked once more without tools
    stubborn = {"parts": [{"type": "tool_call", "id": "cell_late", "name": "run_cell",
                           "args": {"code": "x"}}],
                "stop": "tool", "usage": {"in": 1, "out": 1}}
    w = World([tool_reply(), stubborn, done_reply("summary")],
              cells=[{"ok": True, "prints": [], "last": None, "error": None}])
    out = run(w, maxTurns=1)
    assert out["stop"] == "wrapup" and out["replies"] == ["summary"]
    assert len(w.llm_calls) == 3
    assert [t["name"] for t in w.llm_calls[1]["tools"]] == ["run_cell"]
    assert w.llm_calls[2]["tools"] == []
    retry = w.llm_calls[2]["messages"][-1]
    assert retry["parts"][0] == {"type": "tool_result", "call_id": "cell_late",
                                 "content": "not executed: turn ceiling (1)",
                                 "is_error": True}
    assert "Text only" in retry["parts"][-1]["text"]


def test_length_stop_with_dangling_tool_call_gets_synthetic_result():
    # a max_tokens-truncated reply still carries the tool_call; the
    # wrap-up message must answer it or the provider 400s (ADR-005 §2)
    truncated = {"parts": [{"type": "tool_call", "id": "cell_cut",
                            "name": "run_cell", "args": {"code": "x"}}],
                 "stop": "length", "usage": {"in": 10, "out": 4096}}
    w = World([truncated, done_reply("summary")])
    out = run(w)
    assert out["stop"] == "wrapup" and out["replies"] == ["summary"]
    wrap_msg = w.llm_calls[-1]["messages"][-1]
    assert wrap_msg["role"] == "user"
    first, last = wrap_msg["parts"][0], wrap_msg["parts"][-1]
    assert first == {"type": "tool_result", "call_id": "cell_cut",
                     "content": "not executed: response length limit",
                     "is_error": True}
    assert "response length limit" in last["text"]
    # the wrap-up call's usage is tallied too, not dropped
    assert w.turns[0]["llm"]["inTokens"] == 15
    assert w.turns[0]["llm"]["outTokens"] == 4098


def test_length_stop_text_only_wraps_up_without_results():
    truncated = {"parts": [{"type": "text", "text": "long tex"}],
                 "stop": "length", "usage": {"in": 10, "out": 4096}}
    w = World([truncated, done_reply("summary")])
    out = run(w)
    assert out["stop"] == "wrapup"
    wrap_msg = w.llm_calls[-1]["messages"][-1]
    assert [p["type"] for p in wrap_msg["parts"]] == ["text"]


def test_mailbox_inject_and_soft_break_are_drained_effects():
    w = World([done_reply("wrapped")],
              mailbox=[{"kind": "inject", "text": "also X"}, {"kind": "break"}])
    out = run(w)
    assert out["stop"] == "wrapup"
    first = w.llm_calls[0]["messages"]
    # a mid-run message closes with its own [now: …] line (ADR-005 §5)
    assert any(p.get("text", "").startswith("also X\n\n[now: ")
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
    # the view is the `context` any-ui stamped on the message, handed in
    # by the host as uiContext — nothing is fetched from the space
    w = World([done_reply()])
    run(w, uiContext={"spaceId": "sp9", "objectId": "ob3", "view": "object"})
    call = w.llm_calls[0]
    user = call["messages"][-1]["parts"][0]["text"]
    assert user.startswith("go\n\n[now: ")
    assert user.endswith("user's view — space: sp9, object: ob3, view: object]")
    # runtime context is appended to the system arg, guest-side
    assert "## Runtime context" in call["system"]
    assert "`s1`" in call["system"] and "`c1`" in call["system"]
    # the installed apps ride it (ADR-027 §5): agent space + the
    # user's space, hidden entries out
    assert "- apps in the agent space: Wiki (wiki): A tree of pages\n" in call["system"]
    assert "- apps in the user's space: Wiki (wiki): A tree of pages\n" in call["system"]
    assert "General" not in call["system"].split("## Runtime context")[1]
    # the persisted turn keeps the raw userText — suffix is llm-only
    assert w.turns[0]["userText"] == "go"
    # the same view is bound as cell globals before the loop (ADR-010 §8)
    [prelude] = w.preludes
    assert ("currentUserSpace = {'spaceId': 'sp9', 'objectId': 'ob3', "
            "'view': 'object'}") in prelude
    assert "baoSpaceConfig = {'spaceId': 's1', 'chatId': 'c1'}" in prelude
    # and the prompt names both
    assert "currentUserSpace" in call["system"]
    assert "baoSpaceConfig" in call["system"]


def test_context_suffix_degrades_to_timestamp_without_view():
    for absent in (None, {}, {"spaceId": ""}):
        w = World([done_reply()])
        run(w, uiContext=absent)
        user = w.llm_calls[0]["messages"][-1]["parts"][0]["text"]
        assert "[now: " in user and "user's view" not in user
        assert "currentUserSpace = None" in w.preludes[0]


def test_empty_view_fields_are_dropped_from_the_binding():
    w = World([done_reply()])
    run(w, uiContext={"spaceId": "sp9", "objectId": "", "view": "grid"})
    user = w.llm_calls[0]["messages"][-1]["parts"][0]["text"]
    assert user.endswith("user's view — space: sp9, view: grid]")
    assert "currentUserSpace = {'spaceId': 'sp9', 'view': 'grid'}" in w.preludes[0]


def test_injected_message_carries_its_own_view_and_rebinds_the_global():
    w = World([tool_reply("x = 1"), done_reply()],
              mailbox=[{"kind": "inject", "text": "also here",
                        "context": {"spaceId": "sp2", "objectId": "ob7"}}])
    run(w, uiContext={"spaceId": "sp9"})
    injected = [m for c in w.llm_calls for m in c["messages"]
                if m["role"] == "user"
                and m["parts"][0].get("text", "").startswith("also here")]
    assert injected
    assert injected[0]["parts"][0]["text"].endswith(
        "user's view — space: sp2, object: ob7]")
    # opener bound sp9; the inject rebinds to where the user is NOW
    assert "currentUserSpace = {'spaceId': 'sp9'}" in w.preludes[0]
    assert "currentUserSpace = {'spaceId': 'sp2', 'objectId': 'ob7'}" in w.preludes[-1]


# --- two-tier composition (ADR-009 §2/§3) -------------------------------------

class FakeToolModule:
    """Stands in for a use()'d tool module; describe() is faked to
    return the description carried in the tool fixture row."""

    def __init__(self, desc):
        self.desc = desc


def _helpers():
    # _tool_docs renders each tool via describe(use(spec)) (ADR-010
    # §3): the fake use() resolves "name@vN" / "agent:name@vN" back to
    # the fixture row (source encoded in the description strings), the
    # fake describe() prints it.
    def fake_use(spec):
        if "broken" in spec:
            raise ValueError("boom")
        return FakeToolModule(spec)

    g = {"effect": None, "use": fake_use, "subcell": None,
         **kernel_globals(now=0),
         "describe": lambda mod: f"described:{mod.desc}"}
    exec(compile(SRC, "toolcaller@v1.py", "exec"), g)
    return g


class TwoSpaces:
    """Fake any-client over a code space + a working space."""

    def __init__(self):
        self.skills = {
            "code": [("s1", "_core", "# shipped core"),
                     ("s2", "_extra", "# shipped extra")],
            "user": [("u1", "_core", "# user core")]}
        self.tools = {
            # createdAt as server instants with DIFFERING stamps — equal
            # dicts would hide a raw-dict sort (ADR-019 §Context 2)
            "code": [("p1", "webSearch", "v1", {"$date": 1000}, "searches the web"),
                     ("p2", "shippedOnly", "v1", {"$date": "1970-01-01T00:00:02Z"},
                      "only shipped")],
            "user": [("q1", "webSearch", "v1", {"$date": 9000}, "my patched search")]}
        self.readmes = {"conn": ("r1", "# Connectors\n\nintegrations live here")}

    def list_types(self, space):
        return [{"id": "skillT", "xKey": "agent_skill"},
                {"id": "progT", "xKey": "program"}]

    def query_objects(self, space, filter=None, limit=None, **kw):
        if filter == {"any.types": "skillT"}:
            return [{"id": oid, "any": {"name": name}}
                    for oid, name, _ in self.skills.get(space, [])]
        if filter == {"program.any_tool": True}:
            return [{"id": oid, "createdAt": at,
                     "program": {"name": n, "version": v, "any_tool": True}}
                    for oid, n, v, at, _ in self.tools.get(space, [])]
        if filter == {"any.name": "README"}:
            row = self.readmes.get(space)
            return [{"id": row[0]}] if row else []
        return []

    def query(self, space, oid, dataset, **kw):
        return []

    def get_markdown(self, space, oid):
        for o, _, md in self.skills.get(space, []):
            if o == oid:
                return md
        row = self.readmes.get(space)
        if row and row[0] == oid:
            return row[1]
        return ""

    def get_brain(self):
        return {}


def test_skills_merge_working_space_wins():
    g = _helpers()
    skills = g["_load_system_skills"](TwoSpaces(), "user", "code")
    # user copy shadows the shipped _core; shipped-only _extra survives
    assert skills == {"_core": "# user core", "_extra": "# shipped extra"}


def test_blank_working_space_skill_does_not_shadow():
    g = _helpers()
    two = TwoSpaces()
    two.skills["user"].append(("u2", "_extra", "  \n"))
    skills = g["_load_system_skills"](two, "user", "code")
    assert skills["_extra"] == "# shipped extra"


# --- identity first (ADR-005 §5) ----------------------------------------------

SOUL = "You are Bao. Dry, brief, on their side.\n\n## Voice\n\n- deadpan"


def _souled(user_soul=None):
    two = TwoSpaces()
    two.skills["code"].insert(0, ("s0", "_soul", SOUL))
    if user_soul is not None:
        two.skills["user"].append(("u9", "_soul", user_soul))
    return two


def test_identity_opens_the_system_block_verbatim_and_leaves_the_band():
    g = _helpers()
    system, soul = g["compose_system"](_souled(), "user", "code")
    assert soul == SOUL
    assert system.startswith(SOUL + "\n\n# user core")   # first bytes, no heading
    assert "# Skill: _soul" not in system
    assert "_soul" not in g["SYSTEM_SKILL_ORDER"]


def test_identity_working_space_shadows_blank_falls_back():
    g = _helpers()
    system, soul = g["compose_system"](_souled("You are Bo."), "user", "code")
    assert soul == "You are Bo." and system.startswith("You are Bo.\n\n")
    system, soul = g["compose_system"](_souled("\n  \n"), "user", "code")
    assert soul == SOUL


def test_identity_is_capped_head_kept():
    g = _helpers()
    essay = "word " * 5000                       # ~6k tokens
    two = _souled(essay)
    _, soul = g["compose_system"](two, "user", "code")
    assert soul.startswith("word word") and soul.endswith("shorten the object]")
    assert g["approx_tokens"](soul) <= g["IDENTITY_TOKEN_CAP"] + 20


def test_no_identity_when_asked_quiet_and_when_the_space_has_none():
    g = _helpers()
    system, soul = g["compose_system"](_souled(), "user", "code", identity=False)
    assert soul == "" and system.startswith("# user core")
    system, soul = g["compose_system"](TwoSpaces(), "user", "code")
    assert soul == "" and system.startswith("# user core")


class Souled(World):
    """World whose space ships a `_soul` skill."""

    def use(self, spec):
        mod = super().use(spec)
        if spec == "any@v1":
            mod.list_types = lambda space: [{"id": "skillT", "xKey": "agent_skill"}]
            mod.query_objects = (lambda space, filter=None, **kw:
                                 [{"id": "o1", "any": {"name": "_soul"}}]
                                 if filter == {"any.types": "skillT"} else [])
            mod.get_markdown = lambda space, oid: SOUL
        return mod


def test_run_records_both_fingerprints_and_quiet_composes_no_identity():
    w = Souled([done_reply("hi")])
    run(w)
    assert w.llm_calls[0]["system"].startswith(SOUL)
    llm = w.turns[0]["llm"]
    assert len(llm["soulFingerprint"]) == 16 and len(llm["promptFingerprint"]) == 16
    assert llm["soulFingerprint"] != llm["promptFingerprint"]
    w2 = Souled([done_reply("report")])
    run(w2, quiet=True)
    assert not w2.llm_calls[0]["system"].startswith(SOUL)
    assert "## Subagent" in w2.llm_calls[0]["system"]


def test_skills_degenerate_single_space_reads_once():
    g = _helpers()
    skills = g["_load_system_skills"](TwoSpaces(), "code", "code")
    assert skills == {"_core": "# shipped core", "_extra": "# shipped extra"}


def test_tool_docs_two_tier_prefixes_and_shadows():
    g = _helpers()
    docs = g["_tool_docs"](TwoSpaces(), "user", "code")
    # shipped-only tool imports through the agent: alias; its body is
    # describe(use(spec)) — rendered from code, not from datasets
    assert 'Import: `use("agent:shippedOnly@v1")`' in docs
    assert "described:agent:shippedOnly@v1" in docs
    # the user-space webSearch shadows the shipped one: the DISPLAYED
    # import stays unqualified (cell code resolves it locally), while
    # the render loads space-qualified — compose is overlay module
    # code, whose unqualified use() would miss the working space
    # (ADR-004 §2.4 / ADR-013 §1)
    assert 'Import: `use("webSearch@v1")`' in docs
    assert "described:user:webSearch@v1" in docs
    assert "agent:webSearch" not in docs


def test_tool_docs_degenerate_has_no_prefix():
    g = _helpers()
    docs = g["_tool_docs"](TwoSpaces(), "code", "code")
    assert 'Import: `use("webSearch@v1")`' in docs
    assert "agent:" not in docs
    assert "described:code:webSearch@v1" in docs  # render loads qualified


def test_tool_docs_broken_tool_lists_with_error():
    # a tool whose source fails to load must not sink the compose
    g = _helpers()
    two = TwoSpaces()
    two.tools["code"].append(("p3", "broken", "v1", {"$date": 3000}, "x"))
    docs = g["_tool_docs"](two, "code", "code")
    assert "### broken" in docs
    assert "(unavailable: ValueError: boom)" in docs
    assert "### webSearch" in docs  # the rest still composed


def test_repo_inventory_lists_readme_first_line():
    g = _helpers()
    inv = g["_repo_inventory"](TwoSpaces(), {"conn": "conn", "bare": "bareSpace"})
    assert "## Repos" in inv
    assert "- `conn` (space `conn`) — Connectors" in inv
    assert "- `bare` (space `bareSpace`)" in inv          # no README: still listed
    assert "list_programs" in inv


def test_repo_inventory_empty_overlays_is_absent():
    g = _helpers()
    assert g["_repo_inventory"](TwoSpaces(), {}) == ""


def test_repo_inventory_includes_agent_code_row_and_shadowing_rule():
    g = _helpers()
    inv = g["_repo_inventory"](TwoSpaces(), {"conn": "conn"}, "codeSp1")
    assert "- `agent` (space `codeSp1`)" in inv    # no README: still listed
    assert "- `conn` (space `conn`) — Connectors" in inv
    assert "SHADOW" in inv and 'use("agent:<name>@vN")' in inv
    # without a code space the rule stays out
    assert "SHADOW" not in g["_repo_inventory"](TwoSpaces(), {"conn": "conn"})


def test_runtime_context_names_the_code_space():
    # the agent overlay is a ## Repos row now — not a Runtime-context id line
    w = World([done_reply("hi")])
    run(w, codeSpace="codeSp1", overlays={"conn": "connSp"})
    system = w.llm_calls[0]["system"]
    assert "- `agent` (space `codeSp1`)" in system
    assert 'use("agent:<name>@vN")' in system and "SHADOW" in system
    assert "agent code space" not in system


def test_runtime_context_degenerate_omits_code_line():
    w = World([done_reply("hi")])
    run(w)  # no codeSpace arg → code space == working space, no overlays
    assert "agent code space" not in w.llm_calls[0]["system"]
    assert "## Repos" not in w.llm_calls[0]["system"]


def test_user_skills_lists_titles_and_ids_not_bodies():
    g = _helpers()
    two = TwoSpaces()
    two.skills["user"].append(("u9", "review-pr", "# step one..."))
    out = g["_user_skills"](two, "user")
    assert "## User skills" in out
    assert "- **review-pr** (`u9`)" in out
    assert "step one" not in out           # body stays out of the prompt
    assert "_core" not in out              # system skills excluded
    # a space with only _-skills injects no section at all
    assert g["_user_skills"](TwoSpaces(), "user") == ""


def test_reply_links_auto_attach_and_mentions_stay_text_only():
    # [Name](any://…) destinations in the reply become attachment
    # chips (typed o/, f/, and legacy bare forms; deduped), while
    # mentions and space links stay text-only
    text = ("See [Report](any://o/sp1/obj1) and "
            "[the same](any://o/sp1/obj1) again, "
            "[old style](any://sp1/obj2), "
            "[a file](any://f/sp1/file3), "
            "hey [Zarko](any://m/sp1/idX) — also [Space](any://s/sp1)")
    w = World([done_reply(text)])
    run(w)
    body = w.chat_posts[-1]
    assert body["attachments"] == {
        "a0": {"type": "link", "link": "any://o/sp1/obj1"},
        "a1": {"type": "link", "link": "any://sp1/obj2"},
        "a2": {"type": "link", "link": "any://f/sp1/file3"},
    }


def test_reply_without_links_posts_no_attachments_key():
    w = World([done_reply("plain words only")])
    run(w)
    assert "attachments" not in w.chat_posts[-1]


# --- model profile traits (ADR-005 §1.3) -------------------------------------

def test_malformed_tool_call_gets_error_result_then_retries():
    bad = {"parts": [{"type": "tool_call", "id": "c_bad", "name": "run_cell",
                      "args": {}, "error": "unparseable tool arguments: {oops"}],
           "stop": "tool", "usage": {"in": 10, "out": 5}}
    w = World([bad, tool_reply(), done_reply("ok")])
    out = run(w)
    assert out["stop"] == "done" and out["turns"] == 3
    # no cell ran for the malformed call; the model saw an is_error result
    assert w.spans.count(("begin", "cell")) == 1
    err = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert err["type"] == "tool_result" and err["is_error"]
    assert err["call_id"] == "c_bad" and "unparseable" in err["content"]


def test_malformed_calls_over_budget_wrap_up():
    bad = {"parts": [{"type": "tool_call", "id": "c", "name": "run_cell",
                      "args": {}, "error": "garbage"}],
           "stop": "tool", "usage": {"in": 10, "out": 5}}
    w = World([bad, bad, done_reply("summary")], traits={"malformed_retries": 1})
    out = run(w)
    assert out["stop"] == "wrapup"
    text = w.llm_calls[-1]["messages"][-1]["parts"][-1]["text"]
    assert "malformed tool calls" in text


def test_context_ceiling_counts_cached_prompt_tokens():
    # native anthropic with markers: nearly the whole prompt is a cache
    # hit, usage.in stays tiny — the ceiling must see the cached part
    cached = tool_reply(usage={"in": 2, "out": 5, "cacheRead": 30000, "cacheWrite": 100})
    w = World([cached, done_reply("summary")], traits={"context_window": 32768})
    out = run(w)
    assert out["stop"] == "wrapup"
    text = w.llm_calls[-1]["messages"][-1]["parts"][-1]["text"]
    assert "context window nearly full (30102/32768)" in text


def test_context_window_nearly_full_wraps_up():
    big = tool_reply(usage={"in": 30000, "out": 5})
    w = World([big, done_reply("summary")], traits={"context_window": 32768})
    out = run(w)
    assert out["stop"] == "wrapup"
    text = w.llm_calls[-1]["messages"][-1]["parts"][-1]["text"]
    assert "context window nearly full" in text
    # the boot window budget scaled to the window too (a quarter of it)
    assert w.boot_budgets == [8192]


def test_compact_style_and_last_user_instructions():
    w = World([done_reply()], traits={"prompt_style": "compact",
                                      "instructions_at": "last_user"})
    run(w)
    call = w.llm_calls[0]
    assert "Run a Python cell." in call["tools"][0]["description"]
    assert "## Runtime context" not in call["system"]
    user = call["messages"][-1]["parts"][0]["text"]
    assert user.startswith("go") and "## Runtime context" in user
    assert "chat object: `c1`" in user


def test_full_style_keeps_instructions_in_system():
    w = World([done_reply()])
    run(w)
    call = w.llm_calls[0]
    assert call["tools"][0]["description"].startswith("Execute a Python cell")
    assert "## Runtime context" in call["system"]
    assert "## Runtime context" not in call["messages"][-1]["parts"][0]["text"]

# --- the bash tool (ADR-024 §4, ADR-005 §2 amendment) -------------------------

SHELL_INFO = {"cwd": "/home/u/proj", "home": "/home/u", "shell": "/bin/zsh", "os": "linux"}


class Result:
    def __init__(self, out="", err="", code=0, **kw):
        self.out, self.err, self.code = out, err, code
        self.timed_out = kw.get("timed_out", False)
        self.interrupted = kw.get("interrupted", False)
        self.truncated = kw.get("truncated", False)
        self.duration_ms = kw.get("duration_ms", 7)


class ShellWorld(World):
    """A binary WITH shell effects: runtime.get("shell") resolves, and
    the kernel's value store hands back the ShellResult a bash cell
    left as its last value."""

    def __init__(self, replies, cells=None, results=None, **kw):
        super().__init__(replies, cells=cells, **kw)
        self.results = results or {}
        self.codes = []
        w = self

        class Values:
            @staticmethod
            def get(cid, i="last"):
                return w.results[cid]

        self.values = Values()

    def effect(self, name, payload=None):
        if name == "runtime.get" and payload == {"key": "shell"}:
            return {"value": SHELL_INFO}
        return super().effect(name, payload)

    def subcell(self, code, cell_id):
        if cell_id != "_ctx":
            self.codes.append((cell_id, code))
        return super().subcell(code, cell_id)


def bash_reply(command, cid="b1", **extra):
    return {"parts": [{"type": "tool_call", "id": cid, "name": "bash",
                       "args": {"command": command, **extra}}],
            "stop": "tool", "usage": {"in": 10, "out": 5}}


def test_without_shell_only_run_cell_and_no_coding_skill():
    w = World([done_reply("hi")])
    run(w)
    assert [t["name"] for t in w.llm_calls[0]["tools"]] == ["run_cell"]
    assert "- shell:" not in w.llm_calls[0]["system"]
    g = {"effect": w.effect, "use": w.use, "subcell": w.subcell, **kernel_globals()}
    exec(compile(SRC, "toolcaller@v1.py", "exec"), g)
    skills = {"_core": "core", "_coding": "coding", "_any": "any"}
    assert g["_compose_skills"](skills) == "core\n\nany"
    assert g["_compose_skills"](skills, has_shell=True) == "core\n\nany\n\ncoding"


def test_bash_tool_runs_a_subcell_and_renders_raw():
    cell = {"ok": True, "prints": [],
            "last": {"repr": "…", "size": 1, "schema": "ShellResult"}, "error": None}
    w = ShellWorld([bash_reply("cargo test 2>&1 | tail -3", cwd="/home/u/proj",
                               timeout_s=30, **{"as": "tests"}),
                    done_reply("ok")],
                   cells=[cell],
                   results={"b1": Result("test a ... ok\ntest b ... FAILED\n",
                                         err="warning: unused\n", code=101)})
    run(w)
    assert [t["name"] for t in w.llm_calls[0]["tools"]] == ["run_cell", "bash"]
    assert "- shell: this device (`linux`), serve cwd `/home/u/proj`" in w.llm_calls[0]["system"]
    assert w.codes == [("b1", "tests = sh('cargo test 2>&1 | tail -3', cwd='/home/u/proj', "
                              "timeout_s=30)\ntests")]
    assert ("begin", "bash") in w.spans and ("end", True) in w.spans
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["type"] == "tool_result" and part["call_id"] == "b1"
    assert part["is_error"] is False
    assert part["content"] == ("test a ... ok\ntest b ... FAILED\n"
                               "[stderr]\nwarning: unused\n"
                               "[exit 101]\n"
                               "→ sh.last (also `tests`)")


def test_bash_as_name_is_validated_and_footer_degrades():
    cell = {"ok": True, "prints": [], "last": {"repr": "", "size": 0, "schema": "x"},
            "error": None}
    w = ShellWorld([bash_reply("true", **{"as": "sh"}), done_reply("ok")],
                   cells=[cell], results={"b1": Result("")})
    run(w)
    assert w.codes == [("b1", "sh('true')")]   # a kernel name is never rebound
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["content"] == "(no output)\n→ sh.last"


def test_bash_timeout_and_cell_error():
    cells = [{"ok": True, "prints": [], "last": {"repr": "", "size": 0, "schema": "x"},
              "error": None},
             {"ok": False, "prints": [], "last": None,
              "error": {"type": "EffectError", "message": "OSError: spawn /bin/zsh: nope",
                        "traceback": "tb"}}]
    w = ShellWorld([bash_reply("sleep 9", cid="b1"), bash_reply("x", cid="b2"),
                    done_reply("ok")],
                   cells=cells,
                   results={"b1": Result("partial\n", code=None, timed_out=True,
                                         duration_ms=1200)})
    run(w)
    p1 = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert p1["content"] == "partial\n[timed out after 1200 ms — partial output above]\n→ sh.last"
    p2 = w.llm_calls[2]["messages"][-1]["parts"][0]
    assert p2["is_error"] is True
    assert p2["content"].startswith("Error: EffectError: OSError: spawn")
    assert ("end", False) in w.spans


def test_bash_output_is_clipped_head_and_tail():
    g = {"effect": lambda *a: None, "use": None, "subcell": None, **kernel_globals()}
    exec(compile(SRC, "toolcaller@v1.py", "exec"), g)
    big = "A" * 12000 + "M" * 5000 + "Z" * 4000
    cr = {"ok": True, "prints": [], "last": {}, "error": None}
    text = g["render_bash"](cr, Result(big), None)
    marker = "\n[… 5000 chars elided — the full text is on sh.last.out …]\n"
    assert text.startswith("A" * 12000 + marker)
    assert text.endswith("Z" * 4000 + "\n→ sh.last")
    assert "M" not in text


# --- run_cell mock / mockref (ADR-028 §5) -----------------------------------

def mock_reply(mock=None, mockref=None, code="r = http.get(u)", cid="cell_m"):
    args = {"code": code}
    if mock is not None:
        args["mock"] = mock
    if mockref is not None:
        args["mockref"] = mockref
    return {"parts": [{"type": "tool_call", "id": cid, "name": "run_cell", "args": args}],
            "stop": "tool", "usage": {"in": 10, "out": 5}}


MOCKED_ROWS = [
    {"seq": 11, "effect": "http.get", "class": "read", "mocked": True,
     "unmatched": False, "error": None},
    {"seq": 12, "effect": "http.get", "class": "read", "mocked": True,
     "unmatched": False, "error": None},
    # a facade whose inner effects all came from the mock: a mutation
    # that did NOT execute
    {"seq": 15, "name": "any.modify", "class": "mutate", "mutations": 1,
     "effects": 2, "mocked": 2, "error": None},
    # executed live inside the mockable set: the traceDiff row
    {"seq": 16, "effect": "any.query", "class": "read", "mocked": False,
     "unmatched": True, "error": None},
    # the digest's own read is never counted
    {"seq": 17, "effect": "trace.effects_of", "class": "read", "mocked": False,
     "unmatched": False, "error": None},
]


def test_mockref_rides_the_cell_span_and_the_digest_says_mocked():
    w = World([mock_reply(mockref="run_abc"), done_reply("ok")])
    w.effect_rows = MOCKED_ROWS
    run(w)
    # the spec crossed on the cell span input, sugar expanded
    cell_inputs = [i for i in w.span_inputs if i.get("cell") == "cell_m"]
    assert cell_inputs == [{"cell": "cell_m", "preview": "r = http.get(u)",
                            "mock": {"from": "run_abc"}}]
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["is_error"] is False
    text = part["content"]
    lines = text.split("\n")
    # header ALWAYS first: served / total, sources, per-op, the live list
    assert lines[0] == ("[MOCK] 4 of 5 effects served from run_abc "
                        "(any.modify ×1, http.get ×2); 1 live: any.query #16"), lines[0]
    assert "any.modify ×1 (mocked)" in text
    assert "any.query ×1 (live)" in text
    assert "http.get ×2 (mocked)" in text
    assert "would mutate any.modify #15 (mocked: NOT executed)" in text
    assert "  mutate any.modify" not in text
    # the fixed guard closes every mocked result
    assert text.endswith("Re-run without `mock` to do it for real.")


def test_mock_spec_with_zero_hits_is_visible_and_live_cells_are_untouched():
    w = World([mock_reply(mock={"only": ["http.*"], "records": [
        {"effect": "http.get", "output": {"status": 200}}]}), done_reply("ok")])
    w.effect_rows = [{"seq": 21, "effect": "any.query", "class": "read", "mocked": False,
                      "unmatched": False, "error": None}]
    run(w)
    text = w.llm_calls[1]["messages"][-1]["parts"][0]["content"]
    assert text.startswith("[MOCK] 0 of 1 effects served from 1 inline record(s)"), text
    assert "any.query ×1 (live)" in text
    assert text.endswith("Re-run without `mock` to do it for real.")

    # an ordinary cell: no header, no suffixes, no guard
    w = World([tool_reply(), done_reply("ok")])
    run(w)
    text = w.llm_calls[1]["messages"][-1]["parts"][0]["content"]
    assert "[MOCK]" not in text and "(live)" not in text and "Re-run without" not in text
    assert "mutate http.post #9" in text


def test_mixed_facade_and_multi_row_ops_render_their_split():
    rows = [
        {"seq": 31, "name": "any.create_object", "class": "mutate", "mutations": 1,
         "effects": 3, "mocked": 2, "error": None},
        {"seq": 32, "effect": "http.get", "class": "read", "mocked": True,
         "unmatched": False, "error": None},
        {"seq": 33, "effect": "http.get", "class": "read", "mocked": False,
         "unmatched": True, "error": None},
    ]
    w = World([mock_reply(mock={"from": ["run_a", "run_b"], "unmatched": "live"}),
               done_reply("ok")])
    w.effect_rows = rows
    run(w)
    text = w.llm_calls[1]["messages"][-1]["parts"][0]["content"]
    assert text.startswith("[MOCK] 3 of 5 effects served from run_a, run_b (http.get ×1)"
                           "; 1 live: http.get #33"), text
    assert "any.create_object ×1 (mixed: 2 mocked, 1 live)" in text
    assert "http.get ×2 (mixed)" in text
    # a mixed facade still did not fully execute its mutation
    assert "would mutate any.create_object #31 (mocked: NOT executed)" in text


def test_string_mock_spec_is_rejected_not_run_live():
    # F1 (questionary 09-14): the model passed the spec as a JSON string;
    # dropping it silently ran the cell live under a request for
    # recorded effects
    spec = '{"records": [{"effect": "http.get", "output": {"status": 200}}]}'
    w = World([mock_reply(mock=spec), mock_reply(mockref=["run_a"], cid="cell_n"),
               done_reply("ok")])
    run(w)
    first = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert first["is_error"] is True
    assert "mock must be a JSON object, got str" in first["content"]
    second = w.llm_calls[2]["messages"][-1]["parts"][0]
    assert second["is_error"] is True and "mockref must be a run id string" in second["content"]
    assert ("begin", "cell") not in w.spans   # neither cell ran


def test_zero_match_glob_warns_in_the_header():
    # F2 (questionary 09-14): `only: ["any.*"]` used to be a silent no-op
    rows = [{"seq": 41, "effect": "http.post", "class": "mutate", "mocked": False,
             "unmatched": False, "error": None}]
    w = World([mock_reply(mock={"only": ["any.*"], "unmatched": "fail"}), done_reply("ok")])
    w.effect_rows = rows
    w.mock_filter = {"only": 0}
    run(w)
    text = w.llm_calls[1]["messages"][-1]["parts"][0]["content"]
    lines = text.split("\n")
    assert lines[0] == "[MOCK] 0 of 1 effects served from nothing", lines[0]
    assert lines[1].startswith(
        'WARNING: only: ["any.*"] matched no call in this cell — every effect ran live'), lines[1]
    # a glob that did match: no warning
    w = World([mock_reply(mock={"except": ["any.*"], "mockref": None}), done_reply("ok")])
    w.effect_rows = rows
    w.mock_filter = {"except": 1}
    run(w)
    text = w.llm_calls[1]["messages"][-1]["parts"][0]["content"]
    assert "WARNING" not in text
    # a cell with no effects at all: nothing to warn about
    w = World([mock_reply(mock={"only": ["any.*"]}), done_reply("ok")])
    w.effect_rows = []
    w.mock_filter = {"only": 0}
    run(w)
    assert "WARNING" not in w.llm_calls[1]["messages"][-1]["parts"][0]["content"]


def test_bad_mock_spec_is_an_error_result_before_any_cell():
    w = World([mock_reply(mock={"from": "nope"}), done_reply("ok")])
    w.reject_mock = "mock.from: not a run id: \"nope\""
    out = run(w)
    assert out["stop"] == "done"
    part = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert part["is_error"] is True
    assert part["content"].startswith("Error: mock spec rejected — mock_spec: mock.from")
    # the span never opened, so no cell ran (the rejected call still
    # counts as a tool result, like a malformed call)
    assert ("begin", "cell") not in w.spans
