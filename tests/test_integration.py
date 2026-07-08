"""Wire-contract tests against a REAL any server (`-m integration`).

These pin what offline fakes cannot: the server's own behaviour on the
agentlog v2 surface (server-assigned seq, the stopReason enum, chunk
levels) and the plain trigger datasets. The same scenarios that caught
two live bugs this session — seq-retry-vs-validation and reserved-group
relabeling — are pinned here so they can't regress. Run with a server up
(`any run --addr 127.0.0.1:7009`).

Host composition (deploy → resolve → guest, the runner) is exercised by
the runtime binary in tests/test_rt_e2e.py and by cargo; it is not
re-tested here.
"""

import pytest
from conftest import AnyError

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


# --- trigger datasets (plain, upsert round-trip) -----------------------------

def _upsert(client, space, object_id, dataset, record_id, value):
    return client.call("POST", f"/v1/spaces/{space}/modify", {
        "objectId": object_id, "dataset": dataset,
        "records": [{"id": record_id, "upsert": True,
                     "ops": [{"type": "$set", "path": "", "value": value}]}]})


def test_trigger_datasets_persist_and_read_back(client, fresh_space):
    # The anchor object carries the agent_trigger type so its datasets
    # (agent_triggers / agent_trigger_runs) are writable plain datasets.
    anchor = client.create_object(fresh_space, {"types": ["agent_trigger"]})["objectId"]

    # a trigger definition survives the write→read round-trip whole
    definition = {"name": "memory sweep", "kind": "cron",
                  "spec": {"cron": "0 * * * *"}, "program": "evolve@v1",
                  "args": {"space": fresh_space}, "owner": "inst-A",
                  "enabled": True, "limits": {"fuelPerRun": 5_000_000}}
    _upsert(client, fresh_space, anchor, "agent_triggers", "sweep1", definition)
    loaded = client.query(fresh_space, anchor, "agent_triggers", filter={"id": "sweep1"})
    assert len(loaded) == 1
    got = loaded[0]
    assert got["kind"] == "cron" and got["spec"] == {"cron": "0 * * * *"}
    assert got["owner"] == "inst-A" and got["limits"] == {"fuelPerRun": 5_000_000}

    # a run record persists to the sibling dataset
    run = {"triggerId": "sweep1", "ts": 1_000_000, "status": "ok",
           "durationMs": 42, "fuel": 1234, "traceRef": "run_local_1"}
    _upsert(client, fresh_space, anchor, "agent_trigger_runs", "run1", run)
    runs = client.query(fresh_space, anchor, "agent_trigger_runs", filter={"id": "run1"})
    assert len(runs) == 1 and runs[0]["fuel"] == 1234 and runs[0]["status"] == "ok"
