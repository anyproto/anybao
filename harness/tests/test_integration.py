"""Integration tests against a REAL any server (skipped without one).

These validate the wire contract — the thing offline fakes cannot. The
same scenarios that caught two live bugs this session are pinned here so
they can't regress. Run: `uv run pytest -m integration` with a server up
(`any run --addr 127.0.0.1:7009`).
"""


import pytest
from anybao.helper import Helper, HelperError

pytestmark = pytest.mark.integration


# --- agentlog v2 (server-assigned seq, enum, chunk level) --------------------

def test_server_assigned_seq_monotonic(client, fresh_space):
    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]
    for i in range(3):
        client.append_turn(fresh_space, chat,
                          {"userText": f"q{i}", "replies": [f"a{i}"],
                           "llm": {"stopReason": "done"}})
    turns = client.query(fresh_space, chat, "agent_turns", sort=["seq"])
    assert [t["seq"] for t in turns] == [0, 1, 2]


def test_v2_fields_round_trip(client, fresh_space):
    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]
    client.append_turn(fresh_space, chat, {
        "userText": "hi", "replies": ["yo"], "traceRef": "run_x", "interrupted": True,
        "llm": {"stopReason": "break_hard", "costUsd": 0.02, "fuelUsed": 999, "cells": 4}})
    t = client.query(fresh_space, chat, "agent_turns")[0]
    assert t["traceRef"] == "run_x" and t["interrupted"] is True
    assert t["llm"]["stopReason"] == "break_hard" and t["llm"]["fuelUsed"] == 999


def test_bad_stop_reason_is_clean_400(client, fresh_space):
    from anybao.anyclient import AnyError
    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]
    with pytest.raises(AnyError) as ei:
        client.append_turn(fresh_space, chat, {"userText": "x", "llm": {"stopReason": "end_turn"}})
    assert ei.value.status == 400 and ei.value.code == "agent.turn_invalid"
    # a valid append still works right after (seq not consumed by the reject)
    client.append_turn(fresh_space, chat, {"userText": "ok", "llm": {"stopReason": "done"}})
    assert client.query(fresh_space, chat, "agent_turns")[0]["seq"] == 0


def test_hierarchical_chunk_level(client, fresh_space):
    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]
    client.create_chunk(fresh_space, chat, {
        "summary": "L1 summary", "level": 1, "fromSeq": 0, "toSeq": 9,
        "periodStart": 1700000000, "periodEnd": 1700003600})
    client.create_chunk(fresh_space, chat, {
        "summary": "L2 super", "level": 2, "fromSeq": 0, "toSeq": 0,
        "periodStart": 1700000000, "periodEnd": 1700003600})
    chunks = client.query(fresh_space, chat, "agent_chunks", sort=["seq"])
    assert [(c["seq"], c["level"]) for c in chunks] == [(0, 1), (1, 2)]


# --- helper facades (catalog, nested shape, normalization) -------------------

def test_helper_nested_create_read_roundtrip(client, fresh_space):
    h = Helper(client, default_space=fresh_space)
    h.create_type("Book", xkey="book")
    h.add_property("book", "Author", xkey="author", kind="string")
    h.add_property("book", "Year", xkey="year", kind="number")
    res = h.create_object("book", {"name": "Dune", "book": {"author": "Herbert", "year": 1965}})
    got = h.get_object(res["id"])
    assert got["any"]["name"] == "Dune"              # reserved group unrelabeled
    assert got["book"] == {"author": "Herbert", "year": 1965}  # user type relabeled


def test_helper_no_silent_drop_live(client, fresh_space):
    h = Helper(client, default_space=fresh_space)
    h.create_type("Book", xkey="book")
    with pytest.raises(HelperError, match="not found"):
        h.create_object("book", {"book": {"nonexistent": 1}})


def test_helper_editor_append(client, fresh_space):
    h = Helper(client, default_space=fresh_space)
    oid = client.create_object(fresh_space, {"types": ["editor"]})["objectId"]
    h.append_markdown(oid, "## section\nbody text")
    md = client.get_markdown(fresh_space, oid)
    assert "section" in md and "body text" in md


# --- trigger persistence (TriggerStore over plain datasets) ------------------

def test_trigger_store_persists_and_rolls_up(client, fresh_space):
    from anybao.triggers import RunRecord, Scheduler, Trigger, TriggerStore

    # the anchor object carries the agent_trigger type so its datasets
    # (agent_triggers / agent_trigger_runs) are writable
    anchor = client.create_object(fresh_space, {"types": ["agent_trigger"]})["objectId"]
    store = TriggerStore(client, space=fresh_space, anchor_object_id=anchor)

    t = Trigger(id="sweep1", name="memory sweep", kind="cron",
                spec={"cron": "0 * * * *"}, program="evolve@v1", owner="inst-A",
                limits={"fuelPerRun": 5_000_000})
    store.save(t)

    # reload from the server → definition survives
    loaded = {x.id: x for x in store.load_all()}
    assert "sweep1" in loaded
    got = loaded["sweep1"]
    assert got.kind == "cron" and got.spec == {"cron": "0 * * * *"}
    assert got.owner == "inst-A" and got.limits == {"fuelPerRun": 5_000_000}

    # record a run → run record persists AND the trigger rollup updates
    sched = Scheduler("inst-A", now=lambda: 1000.0)
    rec = sched.record_run(t, status="ok", duration_ms=42, fuel=1234,
                           cost_usd=0.003, trace_ref="run_local_1")
    store.record_run(t, rec, ts_ms=1_000_000)

    runs = store.runs("sweep1")
    assert len(runs) == 1 and runs[0]["fuel"] == 1234 and runs[0]["status"] == "ok"

    # the reloaded trigger carries the rollup (monitoring view, no run open)
    rolled = {x.id: x for x in store.load_all()}["sweep1"]
    assert rolled.run_count == 1 and rolled.last_status == "ok"
    assert rolled.last_run_ref == "run_local_1" and rolled.last_fuel == 1234
