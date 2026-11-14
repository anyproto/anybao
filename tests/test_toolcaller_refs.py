"""toolcaller — the refs of a conversation (ADR-035 §2–§5a): the table
derived at run start from the trace records of the window's runs and
one read for the names, the `getRef` resolver bound in `_ctx`, the refs
line on the message, the chips for what a run changed, and the note on
a cell that carried an unknown handle. The loop holds uris only: no
handle is made here (llm@v1 makes them, per call)."""

import pytest
from kernelenv import kernel_globals
from test_toolcaller import SRC, World, done_reply, run, tool_reply

G = {"effect": lambda *a: None, "use": None, "subcell": None, **kernel_globals()}
exec(compile(SRC, "toolcaller@v1.py", "exec"), G)

GARDEN = "bafyrei" + "a3nqgj" + "a" * 46 + ".16dimysbbxowm"
WORK = "bafyrei" + "wrk222" + "b" * 46 + ".16dimysbbxowm"
DUNE = "bafyrei" + "gyuur4" + "c" * 46
BOOKS = "bafyrei" + "k7mzzz" + "d" * 46
GONE = "bafyrei" + "gone22" + "e" * 46
FILE = "3V2A2AJ6J8j"
DUNE_URI = f"any://o/{GARDEN}/{DUNE}"


def span(run_id, sid, seq, name, inp, out, ok=True):
    """The two rows the minting pipeline gives for one span."""
    return [{"runId": run_id, "span": sid, "seq": seq, "phase": "begin", "name": name,
             "space": inp.get("spaceConfig"), "objectIn": inp.get("object_id"),
             "fileName": inp.get("name")},
            {"runId": run_id, "span": sid, "seq": seq + 1, "phase": "end", "name": name,
             "ok": ok, "objectOut": out.get("objectId"), "fileId": out.get("fileId")}]


def world(replies, **kw):
    w = World(replies, **kw)
    w.spaces = [{"id": GARDEN, "name": "Garden"}, {"id": WORK, "name": "Work"}]
    w.objects = [
        {"id": DUNE, "spaceId": GARDEN, "any": {"name": "Dune: Part One", "type": "book"}},
        {"id": BOOKS, "spaceId": GARDEN, "any": {"name": "Books", "type": "folder"}},
    ]
    w.past_turns = [
        {"seq": 1, "traceRef": "run_1", "userText": "add Dune",
         "context": f"[now: Thu 2026-11-12 10:00 | user's view — space: {WORK}]",
         "replies": [f"Added to [Books](any://o/{GARDEN}/{BOOKS})."]},
        {"seq": 2, "traceRef": "run_2", "userText": "this is the cover",
         "replies": ["Attached the cover to Dune."]},
    ]
    w.trace_records = [
        # run 1 created Dune in a space named by name, and wrote a page
        # that has since been deleted; one write failed
        *span("run_1", "s1", 10, "any.create_object",
              {"spaceConfig": "Garden", "body": {"name": "Dune"}}, {"objectId": DUNE}),
        *span("run_1", "s2", 20, "any.put_markdown",
              {"spaceConfig": GARDEN, "object_id": GONE}, {}),
        *span("run_1", "s3", 30, "any.update_object",
              {"spaceConfig": WORK, "object_id": BOOKS}, {}, ok=False),
        # run 2 attached a file to Dune through a bound global
        *span("run_2", "s1", 10, "any.attach_file",
              {"spaceConfig": {"spaceId": GARDEN}, "object_id": DUNE, "name": "demo.png"},
              {"fileId": FILE, "uri": f"any://f/{GARDEN}/{FILE}"}),
        # another run's write is not this window's
        *span("run_9", "s1", 10, "any.create_object",
              {"spaceConfig": GARDEN}, {"objectId": "bafyrei" + "f" * 52}),
    ]
    return w


def refs_of(w, **args):
    out = run(w, **args)
    return out, w.llm_calls[0]["messages"][-1]["parts"][0]["text"], w.preludes[0]


# --- derive (§2) --------------------------------------------------------------

def test_refs_come_from_the_window_runs_and_are_named_by_one_read():
    w = world([done_reply("ok")])
    _out, text, _ = refs_of(w, uiContext={"spaceId": GARDEN})
    line = next(ln for ln in text.splitlines() if ln.startswith("[refs: "))
    assert line == (
        "[refs: "
        f"[demo.png](any://f/{GARDEN}/{FILE}) on Dune: Part One · "     # newest run first
        f"[Dune: Part One]({DUNE_URI}#book) in Garden · "               # the name of today
        f"[Books](any://o/{GARDEN}/{BOOKS}#folder) in Garden · "        # a reply link
        f"[Garden](any://s/{GARDEN})]")
    # the deleted object, the failed write and the other run's object are absent
    assert GONE not in text and "ffffff" not in text
    # one query over the runs of the window, then one for the chips of this run
    match = w.trace_queries[0]["pipeline"][0]["$match"]
    assert match["runId"] == {"$in": ["run_1", "run_2"]}
    assert "any.create_object" in match["name"]["$in"]
    assert "ref:" not in text                        # the loop never makes a handle


def test_refs_line_rides_the_message_after_the_stamp_and_nowhere_earlier():
    w = world([done_reply("ok")])
    _out, text, _ = refs_of(w, uiContext={"spaceId": GARDEN})
    assert text.index("[now: ") < text.index("[refs: ")
    call = w.llm_calls[0]
    assert "[refs: " not in call["system"]
    assert all("[refs: " not in p.get("text", "")
               for m in call["messages"][:-1] for p in m["parts"])
    # llm-only: the persisted turn keeps the raw stamp
    assert "[refs: " not in w.turns[0]["context"]


def test_a_definition_marker_is_not_a_type_fragment():
    table = [{"kind": "object", "spaceId": GARDEN, "objectId": BOOKS, "name": "General",
              "type": "__type__", "uri": f"any://o/{GARDEN}/{BOOKS}"},
             {"kind": "object", "spaceId": GARDEN, "objectId": DUNE, "name": "Dune",
              "type": "book", "uri": DUNE_URI}]
    line = G["_refs_line"](table)
    assert f"[General](any://o/{GARDEN}/{BOOKS})" in line and "__type__" not in line
    assert f"[Dune]({DUNE_URI}#book)" in line


def test_a_conversation_that_touched_nothing_has_no_refs_line():
    w = World([done_reply("ok")])
    _out, text, prelude = refs_of(w)
    assert "[refs: " not in text
    assert "_refs_table = []" in prelude
    assert w.trace_queries == [{"coll": "records", "pipeline": w.trace_queries[0]["pipeline"]}]
    assert w.trace_queries[0]["pipeline"][0]["$match"]["runId"] == "run_x"   # the chips read


def test_the_query_lifts_the_ids_out_and_leaves_the_bodies():
    w = world([done_reply("ok")])
    run(w)
    project = w.trace_queries[0]["pipeline"][1]["$project"]
    # any-store has no dotted output field: each id is a renamed top-level one
    assert all("." not in k for k in project)
    assert project["space"] == "$input.spaceConfig"
    assert project["objectOut"] == "$output.objectId"
    assert "input" not in project and "output" not in project


# --- getRef (§4) --------------------------------------------------------------

def get_ref(table):
    ns = {}
    exec(G["_refs_ctx"](table), ns)
    return ns["getRef"]


TABLE = [
    {"kind": "object", "type": "book", "name": "Dune", "spaceId": GARDEN,
     "objectId": DUNE, "uri": DUNE_URI},
    {"kind": "object", "type": "page", "name": "Notes", "spaceId": GARDEN,
     "objectId": BOOKS, "uri": f"any://o/{GARDEN}/{BOOKS}"},
    {"kind": "object", "type": "page", "name": "Notes", "spaceId": WORK,
     "objectId": GONE, "uri": f"any://o/{WORK}/{GONE}"},
    {"kind": "file", "name": "demo.png", "spaceId": GARDEN, "fileId": FILE,
     "on": DUNE, "uri": f"any://f/{GARDEN}/{FILE}"},
    {"kind": "space", "name": "Garden", "spaceId": GARDEN},
]


def test_get_ref_by_uri_in_and_out_of_the_table():
    g = get_ref(TABLE)
    assert g(DUNE_URI) == TABLE[0]
    assert g(DUNE_URI + "/editor_blocks/b1")["objectId"] == DUNE
    assert g(f"any://f/{GARDEN}/{FILE}")["name"] == "demo.png"
    other = "bafyrei" + "z" * 52
    assert g(f"any://o/{WORK}/{other}") == {
        "kind": "object", "spaceId": WORK, "objectId": other,
        "uri": f"any://o/{WORK}/{other}"}


def test_get_ref_takes_a_space_id_and_a_space_link():
    g = get_ref(TABLE)
    assert g(GARDEN) == TABLE[4]
    assert g(f"any://s/{GARDEN}") == TABLE[4]
    assert g(WORK) == {"kind": "space", "spaceId": WORK}


def test_get_ref_by_name_unique_ambiguous_and_missing():
    g = get_ref(TABLE)
    assert g("dune") == TABLE[0]
    assert g("demo.png")["uri"] == f"any://f/{GARDEN}/{FILE}"
    with pytest.raises(KeyError) as e:
        g("Notes")
    assert f"any://o/{GARDEN}/{BOOKS}" in str(e.value) and f"any://o/{WORK}/{GONE}" in str(e.value)
    with pytest.raises(KeyError) as e:
        g("Dun")
    assert "'Dune'" in str(e.value)


def test_get_ref_raises_on_an_unknown_handle():
    g = get_ref(TABLE)
    for bad in ("ref:unknown/gyx", "ref:object/gyu"):
        with pytest.raises(KeyError) as e:
            g(bad)
        assert "unknown ref" in str(e.value)


def test_the_prelude_binds_get_ref_over_the_table():
    w = world([done_reply("ok")])
    _out, _text, prelude = refs_of(w)
    assert "def getRef(x):" in prelude and DUNE_URI in prelude
    assert "baoSpaceConfig" in prelude


# --- chips for what the run changed (§5a) -------------------------------------

def test_final_reply_chips_what_the_run_changed_and_did_not_link():
    w = world([done_reply(f"Attached the cover to [Books](any://o/{GARDEN}/{BOOKS}).")])
    w.trace_records += span("run_x", "s1", 10, "any.update_object",
                            {"spaceConfig": "Garden", "object_id": DUNE}, {})
    w.trace_records += span("run_x", "s2", 20, "any.update_object",
                            {"spaceConfig": GARDEN, "object_id": BOOKS}, {})
    run(w)
    links = [a["link"] for a in w.chat_posts[-1]["attachments"].values()]
    assert links == [f"any://o/{GARDEN}/{BOOKS}", DUNE_URI]   # the linked one once


def test_chips_for_a_bulk_run_stop_at_the_cap():
    w = world([done_reply("Made forty.")])
    for i in range(40):
        oid = "bafyrei" + f"{i:02d}".replace("0", "a").replace("1", "b").replace(
            "8", "c").replace("9", "d") + "q" * 50
        w.trace_records += span("run_x", f"s{i}", 10 * i, "any.create_object",
                                {"spaceConfig": GARDEN}, {"objectId": oid})
    run(w)
    assert len(w.chat_posts[-1]["attachments"]) == G["REFS_CHIP_CAP"]


def test_an_interim_bubble_gets_no_changed_chips():
    w = world([tool_reply() | {"parts": [{"type": "text", "text": "working"},
                                         *tool_reply()["parts"]]},
               done_reply("done")])
    w.trace_records += span("run_x", "s1", 10, "any.update_object",
                            {"spaceConfig": GARDEN, "object_id": DUNE}, {})
    run(w)
    assert "attachments" not in w.chat_posts[0]
    assert [a["link"] for a in w.chat_posts[-1]["attachments"].values()] == [DUNE_URI]


# --- a handle the wall could not expand (§5) ----------------------------------

def test_a_cell_that_carried_an_unknown_handle_says_so():
    bad = tool_reply('getRef("ref:unknown/gyx")', cid="c1")
    bad["unresolvedRefs"] = ["ref:object/gyx"]
    w = world([bad, done_reply("ok")])
    out = run(w)
    result = w.llm_calls[1]["messages"][-1]["parts"][0]
    assert result["content"].startswith("Unknown ref: ref:object/gyx")
    assert out["unresolvedRefs"] == ["ref:object/gyx"]


def test_a_run_without_unknown_handles_has_no_such_field():
    w = world([tool_reply(), done_reply("ok")])
    out = run(w)
    assert "unresolvedRefs" not in out
    assert not w.llm_calls[1]["messages"][-1]["parts"][0]["content"].startswith("Unknown")
