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
    from anybao.triggers import Scheduler, Trigger, TriggerStore

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


# --- deploy + resolve (programs live in spaces, end to end) ------------------

def test_deploy_program_then_resolve(client, fresh_space):
    from anybao.deploy import Deployer, ProgramSource
    from anybao.modules import AnyModuleResolver

    code = "GREETING = 'hi'\n\ndef greet(name):\n    return GREETING + ' ' + name\n"
    md = ("## Tool Description\n\nGreets.\n\n"
          "## Tool Schema\n### greet(name) [getter]\n\nreturns a greeting.\n")
    prog = ProgramSource("greeter", "v1", code=code, tool_md=md)
    dep = Deployer(client, space=fresh_space)
    assert dep.deploy_one(prog) == "created"
    assert dep.deploy_one(prog) == "unchanged"          # hash-gate, live

    # resolve it back
    r = AnyModuleResolver(client, current_space=fresh_space)
    resolved = r("greeter@v1")
    assert "def greet" in resolved.source
    assert resolved.source_hash.startswith("sha256:")


def test_deploy_then_use_in_wasi_guest(client, fresh_space):
    """The full chain: deploy → AnyModuleResolver → use() in the guest."""
    from pathlib import Path

    from anybao.deploy import Deployer, ProgramSource
    from anybao.modules import AnyModuleResolver
    from anyrt import trace as tr
    from anyrt.builtin_effects import register_builtin_effects
    from anyrt.effects import Broker, Registry
    from anyrt.wasi import WasiEngine

    kernel = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"
    if not kernel.exists():
        pytest.skip("bin/kernel.wasm missing — run `make kernel`")

    Deployer(client, space=fresh_space).deploy_one(ProgramSource(
        "greeter", "v1", "def greet(name):\n    return 'hi ' + name\n"))

    reg = Registry()
    register_builtin_effects(reg, resolver=AnyModuleResolver(client, current_space=fresh_space))
    broker = Broker(reg, tr.TraceWriter(run={"id": "deploy-e2e"}))
    eng = WasiEngine(broker, kernel_wasm=kernel)
    r = eng.run_cell("g = use('greeter@v1')\ng.greet('bao')", cell_id="c1")
    assert r.ok and r.last_value.repr == "'hi bao'"


# --- the runner: full adapter (loop + wasi guest + effects) end to end -------

def test_runner_full_conversation(client, fresh_space):
    """THE keystone: a real conversation through the wasi guest with a
    scripted llm transport (no API key) — cell runs in the sandbox, turn
    persists, trace written device-local."""
    from pathlib import Path

    from anybao.config import Config, DictConfigStore
    from anybao.modules import AnyModuleResolver
    from anybao.runner import Runner

    kernel = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"
    if not kernel.exists():
        pytest.skip("bin/kernel.wasm missing — run `make kernel`")

    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]

    cfg = Config(DictConfigStore({
        "llm.tier.codegen": {"value": {"provider": "anthropic", "model": "test-model",
                                       "base_url": "http://x", "api_key_ref": "llm.key"}},
        "llm.key": {"localValue": "fake"},
    }))

    # scripted anthropic-shaped responses: run a cell, then finish
    calls = []
    def fake_transport(prov, req):
        calls.append(req)
        if len(calls) == 1:
            return {"content": [{"type": "tool_use", "id": "t1", "name": "run_cell",
                                 "input": {"code": "result = 40 + 2\nresult"}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5}}
        return {"content": [{"type": "text", "text": "The answer is 42."}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 3}}

    import tempfile
    traces = Path(tempfile.mkdtemp())
    runner = Runner(client, cfg, kernel_wasm=kernel, traces_dir=traces,
                    resolver=AnyModuleResolver(client, current_space=fresh_space),
                    user_space=fresh_space, llm_transport=fake_transport)

    result = runner.run_conversation(chat, "what is 40 + 2?")

    # the loop finished with the model's final reply
    assert result.outcome.stop == "done"
    assert result.outcome.replies == ["The answer is 42."]
    assert len(calls) == 2  # one tool turn + one done turn

    # the turn persisted to the user space (agent_turns), server-assigned seq
    turns = client.query(fresh_space, chat, "agent_turns", sort=["seq"])
    assert len(turns) == 1
    assert turns[0]["userText"] == "what is 40 + 2?"
    assert turns[0]["traceRef"] == result.trace_ref
    assert turns[0]["replies"] == ["The answer is 42."]

    # the trace is device-local (a file, not synced)
    assert (traces / f"{result.trace_ref}.jsonl").exists()


def test_runner_runs_a_program(client, fresh_space):
    """The trigger execution path: deploy a program with main(), run it."""
    from pathlib import Path

    from anybao.config import Config, DictConfigStore
    from anybao.deploy import Deployer, ProgramSource
    from anybao.modules import AnyModuleResolver
    from anybao.runner import Runner

    kernel = Path(__file__).resolve().parents[2] / "bin" / "kernel.wasm"
    if not kernel.exists():
        pytest.skip("bin/kernel.wasm missing — run `make kernel`")

    Deployer(client, space=fresh_space).deploy_one(ProgramSource(
        "adder", "v1", "def main(args):\n    return args['a'] + args['b']\n"))

    import tempfile
    cfg = Config(DictConfigStore({}))
    runner = Runner(client, cfg, kernel_wasm=kernel, traces_dir=Path(tempfile.mkdtemp()),
                    resolver=AnyModuleResolver(client, current_space=fresh_space),
                    user_space=fresh_space)
    res = runner.run_program("adder@v1", {"a": 3, "b": 4})
    assert res.status == "ok" and res.fuel and res.fuel > 0
