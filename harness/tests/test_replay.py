"""Retrospective replay (ADR-001 §5 mock mode, M6) — fully offline:
synthetic trace via TraceWriter, injected fake executor, no engine."""

import pytest
from anybao.replay import (
    RerunResult,
    cell_code,
    load_trace,
    rerun_cell,
    rerun_turn,
    turns_of,
)
from anyrt import trace as tr
from anyrt.effects import EffectDef, EffectError, Registry
from anyrt.executor import CellResult

CODE_1 = 'r = fetch("http://x")\nany_search("notes")'
CODE_2 = "print(r)"
BIG_BODY = "y" * 200


def llm_output(parts, stop="tool"):
    return {"parts": parts, "stop": stop, "usage": {"in": 10, "out": 5}}


def make_trace(blob_threshold: int = tr.BLOB_THRESHOLD) -> tr.TraceWriter:
    """turn 1: two cells (fetch + data effect, then a print cell);
    turn 2: plain-text done. Keys are REAL input_key hashes so the
    rerun broker's keying matches the recorded records."""
    w = tr.TraceWriter(
        run={"id": "run_old", "program": "toolcaller", "chatId": "c1"},
        blob_threshold=blob_threshold,
    )
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": [], "tier": "codegen"},
        key=tr.input_key("llm.chat", {"messages": [], "tier": "codegen"}),
        output=llm_output([
            {"type": "text", "text": "Working."},
            {"type": "tool_call", "id": "cell_a", "name": "run_cell",
             "args": {"code": CODE_1}},
            {"type": "tool_call", "id": "cell_b", "name": "run_cell",
             "args": {"code": CODE_2}},
        ]),
        meta={"durMs": 320, "class": "read", "usage": {"in": 10, "out": 5}},
    )
    w.effect(
        effect="fetch", cell="cell_a",
        input={"url": "http://x"}, key=tr.input_key("fetch", {"url": "http://x"}),
        output={"status": 200, "body": BIG_BODY},
        meta={"durMs": 88, "class": "read"},
    )
    w.effect(
        effect="any.search", cell="cell_a",
        input={"query": "notes"}, key=tr.input_key("any.search", {"query": "notes"}),
        output={"hits": [{"id": "obj1"}]},
        meta={"durMs": 12, "class": "read"},
    )
    w.cell(cell="cell_a", ok=True, metrics={"fuel_used": 1000, "duration_ms": 40})
    w.cell(cell="cell_b", ok=True, metrics={"fuel_used": 50, "duration_ms": 2})
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": ["…"], "tier": "codegen"},
        key=tr.input_key("llm.chat", {"messages": ["…"], "tier": "codegen"}),
        output=llm_output([{"type": "text", "text": "Done."}], stop="done"),
        meta={"usage": {"in": 20, "out": 7}},
    )
    return w


class DrivingExecutor:
    """Fake executor that actually drives the broker (unlike
    anyrt.executor.FakeExecutor, which executes nothing): each entry
    of `script` is (effect, payload) calls to issue for that cell."""

    def __init__(self, broker, script):
        self.broker = broker
        self.script = list(script)
        self.codes: list[str] = []
        self.outputs: list = []
        self.closed = False

    def run_cell(self, code: str, *, cell_id: str, timeout_s: float | None = None) -> CellResult:
        self.codes.append(code)
        calls = self.script.pop(0) if self.script else []
        for name, payload in calls:
            self.outputs.append(self.broker.call(name, payload))
        return CellResult(cell_id=cell_id, ok=True, duration_ms=1)

    def interrupt(self) -> None:
        pass

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def dump(w: tr.TraceWriter, tmp_path):
    path = tmp_path / "run_old.jsonl"
    w.dump(path)
    return path


def test_load_trace_round_trips(tmp_path):
    w = make_trace()
    records = load_trace(dump(w, tmp_path))
    assert records == w.records


def test_load_trace_hydrates_blob_sidecar(tmp_path):
    w = make_trace(blob_threshold=64)  # fetch output (200-char body) spills
    spilled = w.records[2]["output"]
    assert set(spilled) == {"__blob", "bytes"} and w.blobs
    records = load_trace(dump(w, tmp_path))
    assert records[2]["output"] == {"status": 200, "body": BIG_BODY}
    # non-effect records and unspilled fields untouched
    assert records[0] == w.records[0]
    assert records[2]["input"] == {"url": "http://x"}


def test_turns_of_finds_turns_and_cells(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    turns = turns_of(records)
    assert [t["n"] for t in turns] == [1, 2]
    assert turns[0]["record"]["effect"] == "llm.chat"
    assert [c["cell"] for c in turns[0]["cells"]] == ["cell_a", "cell_b"]
    assert turns[0]["cells"][0]["end"]["ok"] is True
    assert turns[1]["cells"] == []


def test_cell_code_recovers_exact_recorded_code(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    assert cell_code(records, "cell_a") == CODE_1
    assert cell_code(records, "cell_b") == CODE_2
    with pytest.raises(KeyError):
        cell_code(records, "cell_nope")


def test_rerun_cell_serves_recorded_outputs_from_mock_index(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    executors = []

    def factory(broker):
        ex = DrivingExecutor(broker, [
            [("fetch", {"url": "http://x"}), ("any.search", {"query": "notes"})],
        ])
        executors.append(ex)
        return ex

    rr = rerun_cell(records, "cell_a", executor_factory=factory)
    (ex,) = executors
    assert isinstance(rr, RerunResult) and rr.ok and rr.cell == "cell_a"
    assert ex.codes == [CODE_1]                      # recovered recorded code
    assert ex.outputs == [{"status": 200, "body": BIG_BODY}, {"hits": [{"id": "obj1"}]}]
    assert ex.closed
    # rerun trace: two mocked effects + the cell checkpoint, attributed
    effs = [r for r in rr.records if r["kind"] == "effect"]
    assert [r["effect"] for r in effs] == ["fetch", "any.search"]
    assert all(r["meta"]["mocked"] and r["cell"] == "cell_a" for r in effs)
    assert rr.records[-1]["kind"] == "cell" and rr.records[-1]["ok"] is True
    assert rr.diff == []                             # nothing executed live


def test_rerun_cell_hydrates_spilled_outputs(tmp_path):
    records = load_trace(dump(make_trace(blob_threshold=64), tmp_path))

    def factory(broker):
        return DrivingExecutor(broker, [[("fetch", {"url": "http://x"})]])

    rr = rerun_cell(records, "cell_a", executor_factory=factory)
    assert rr.ok  # spilled output came back as the real value via load_trace


def test_rerun_cell_unmatched_fails_by_default(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))

    def factory(broker):
        return DrivingExecutor(broker, [[("fetch", {"url": "http://OTHER"})]])

    with pytest.raises(EffectError) as ei:
        rerun_cell(records, "cell_a", executor_factory=factory)
    assert ei.value.type == "unmatched_mock"


def test_rerun_cell_unmatched_live_executes_and_shows_in_diff(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    reg = Registry()
    reg.add(EffectDef(name="fetch", fn=lambda ctx, url: {"status": 999},
                      kind="read", normalize=None, redact=(), cap="fetch"))

    executors = []

    def factory(broker):
        ex = DrivingExecutor(broker, [
            [("fetch", {"url": "http://x"}), ("fetch", {"url": "http://OTHER"})],
        ])
        executors.append(ex)
        return ex

    rr = rerun_cell(records, "cell_a", executor_factory=factory,
                    registry=reg, unmatched="live")
    assert executors[0].outputs == [{"status": 200, "body": BIG_BODY}, {"status": 999}]
    (live,) = rr.diff                                # traceDiff = the live call
    assert live["effect"] == "fetch" and live["input"] == {"url": "http://OTHER"}


def test_rerun_turn_reruns_every_cell_in_order(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    executors = []

    def factory(broker):
        ex = DrivingExecutor(broker, [
            [("fetch", {"url": "http://x"})],        # cell_a
            [],                                       # cell_b (no effects)
        ])
        executors.append(ex)
        return ex

    results = rerun_turn(records, 1, executor_factory=factory)
    (ex,) = executors                                # one shared executor
    assert ex.codes == [CODE_1, CODE_2]
    assert [r.cell for r in results] == ["cell_a", "cell_b"]
    assert all(r.ok for r in results)
    with pytest.raises(IndexError):
        rerun_turn(records, 3, executor_factory=factory)


def test_rerun_needs_engine_or_factory(tmp_path):
    records = load_trace(dump(make_trace(), tmp_path))
    with pytest.raises(ValueError, match="kernel_wasm"):
        rerun_cell(records, "cell_a")
