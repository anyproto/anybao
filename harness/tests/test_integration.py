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
    obj = h.create_object("Pages" if False else None, {"name": "doc"}) \
        if False else client.create_object(fresh_space, {"types": ["editor"]})
    oid = obj["id"] if "id" in obj else obj["objectId"]
    h.append_markdown(oid, "## section\nbody text")
    md = client.get_markdown(fresh_space, oid)
    assert "section" in md and "body text" in md
