"""Retrospective replay — ADR-001 §5 mock mode as tooling: "re-run
turn 3 of yesterday's run with mocks" (00-plan Phase 4, M6).

A recorded run is re-executed piecewise against its own trace: the
cell CODE is recovered from the recorded `llm.chat` tool_call args,
and effects are served from a `MockIndex` over the old records
(loose (effect, key) match, FIFO per key). Unmatched calls fail by
default (`unmatched="fail"`) or execute live (`"live"`) — the
`trace_diff` view of the rerun trace is exactly the live ones.

Pure parts (record slicing, mock-index construction, code recovery)
need no engine; `executor_factory` injects the executor so tests run
offline, while the default builds a real WasiEngine from
`kernel_wasm`. Kernel state from EARLIER turns is not rebuilt — this
is the accepted primitive semantics: effects replay, guest-local
variables from prior cells do not exist.

Limitation: the mock key is `input_key(effect, canonical_input)`.
The default `registry_from_trace` registers identity stubs (no
normalizer, no redaction), which matches every effect registered
today; for effects with a normalizer/redaction (e.g. `http.*`
Authorization redaction) pass the real `registry=` so re-issued raw
args canonicalize to the recorded key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from anyrt import trace as tr
from anyrt.blobstore import FileSidecarStore
from anyrt.effects import Broker, EffectDef, EffectError, Registry
from anyrt.executor import CellResult

from .viewer import LLM_EFFECT, build_view

# ---- loading (hydrated: spilled values come back inline) ---------------------

def _is_blob_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"__blob", "bytes"}


def _hydrate(rec: dict, blobs: dict[str, str]) -> dict:
    if rec.get("kind") != "effect":
        return rec
    out = dict(rec)
    for field in ("input", "output"):
        v = out.get(field)
        if _is_blob_ref(v) and v["__blob"] in blobs:
            out[field] = tr.resolve_blobs(v, blobs)
    return out


def load_trace(path: Path | str) -> list[dict]:
    """Load a trace + its blob sidecar (FileSidecarStore conventions);
    spilled inputs/outputs hydrate back inline so slicing, mock
    matching and code recovery see the real values (ADR-001 §7:
    'replay resolves refs transparently')."""
    p = Path(path)
    blobs = FileSidecarStore().load(p)
    return [_hydrate(r, blobs) for r in tr.load(p)]


# ---- record slicing (pure) ---------------------------------------------------

def turns_of(records: list[dict]) -> list[dict]:
    """The llm.chat effect records in order (turn N = Nth llm.chat,
    same numbering as the #turn_N anchors), each with the cell
    executions that followed it: `{"n", "record", "cells":
    [{"cell", "code", "end"}]}` — cell ids from the run_cell
    tool_call parts, `end` = the terminal cell record (ADR-001 §4b)."""
    view = build_view(records)
    return [
        {
            "n": t.n,
            "record": t.record,
            "cells": [{"cell": c.cell_id, "code": c.code, "end": c.end} for c in t.cells],
        }
        for t in view.turns
    ]


def cell_code(records: list[dict], cell_id: str) -> str:
    """Recover a cell's exact recorded code from the llm.chat
    tool_call args that announced it."""
    for r in records:
        if r.get("kind") != "effect" or r.get("effect") != LLM_EFFECT:
            continue
        output = r.get("output")
        if not isinstance(output, dict):
            continue
        for part in output.get("parts", []):
            if part.get("type") == "tool_call" and part.get("id") == cell_id:
                code = part.get("args", {}).get("code")
                if code is not None:
                    return code
    raise KeyError(f"no run_cell tool_call for cell {cell_id!r} in trace")


def registry_from_trace(records: list[dict]) -> Registry:
    """Stub registry covering every effect name seen in the trace —
    enough for the broker to key + mock-match calls. The stub fn only
    runs for UNMATCHED calls in `unmatched='live'` mode, where it
    refuses loudly: live execution needs the real `registry=`."""
    reg = Registry()
    kinds: dict[str, str] = {}
    for r in records:
        if r.get("kind") == "effect":
            kinds.setdefault(r["effect"], r.get("meta", {}).get("class") or "read")

    def make_stub(name: str):
        def stub(ctx, **payload):
            raise EffectError(
                name, "not_replayable",
                f"{name}: live execution needs a real registry (stub from trace)",
            )
        return stub

    for name, kind in kinds.items():
        reg.add(EffectDef(
            name=name, fn=make_stub(name),
            kind=kind if kind in ("read", "mutate") else "read",
            normalize=None, redact=(), cap=name,
        ))
    return reg


# ---- re-execution ------------------------------------------------------------

@dataclass
class RerunResult:
    """One re-executed cell: the fresh CellResult plus the rerun-trace
    records written for it (mocked effects carry meta.mocked=true;
    `diff` = the calls that actually executed — ADR-001 §5)."""

    cell: str
    result: CellResult
    records: list[dict]

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def error(self):
        return self.result.error

    @property
    def diff(self) -> list[dict]:
        return tr.trace_diff(self.records)


def _mock_broker(
    records: list[dict],
    *,
    registry: Registry | None,
    unmatched: Literal["fail", "live"],
) -> tuple[Broker, tr.TraceWriter]:
    header = records[0] if records and records[0].get("kind") == "header" else {}
    orig = (header.get("run") or {}).get("id")
    writer = tr.TraceWriter(run={"id": f"rerun:{orig}", "replayOf": orig, "mode": "mock"})
    broker = Broker(
        registry or registry_from_trace(records),
        writer,
        mode="mock",
        mock_index=tr.MockIndex(records),
        mock_unmatched=unmatched,
    )
    return broker, writer


def _make_executor(broker: Broker, executor_factory, kernel_wasm):
    if executor_factory is not None:
        return executor_factory(broker)
    if kernel_wasm is None:
        raise ValueError("rerun needs kernel_wasm (or an injected executor_factory)")
    from anyrt.wasi import WasiEngine

    return WasiEngine(broker, kernel_wasm=kernel_wasm)


def _rerun_one(
    records: list[dict],
    cell_id: str,
    broker: Broker,
    writer: tr.TraceWriter,
    executor,
) -> RerunResult:
    code = cell_code(records, cell_id)
    start = len(writer.records)
    broker.current_cell = cell_id  # injected executors need attribution too
    try:
        result = executor.run_cell(code, cell_id=cell_id)
    finally:
        broker.current_cell = None
    new = writer.records[start:]
    if not any(r.get("kind") == "cell" and r.get("cell") == cell_id for r in new):
        # WasiEngine writes the checkpoint itself; cover injected executors
        err = (
            {"type": result.error.type, "message": result.error.message}
            if result.error else None
        )
        broker.cell_done(
            cell=cell_id, ok=result.ok, error=err,
            interrupted=result.interrupted, metrics=result.metrics,
        )
    return RerunResult(cell=cell_id, result=result, records=writer.records[start:])


def rerun_cell(
    records: list[dict],
    cell_id: str,
    *,
    executor_factory=None,
    registry: Registry | None = None,
    unmatched: Literal["fail", "live"] = "fail",
    kernel_wasm: str | Path | None = None,
) -> RerunResult:
    """Re-execute ONE recorded cell's code with effects mocked from the
    trace (ADR-001 §5 loose mode). `executor_factory(broker) ->
    Executor` injects the engine (tests: a fake that drives the
    broker); default builds a WasiEngine over `kernel_wasm`."""
    broker, writer = _mock_broker(records, registry=registry, unmatched=unmatched)
    executor = _make_executor(broker, executor_factory, kernel_wasm)
    try:
        return _rerun_one(records, cell_id, broker, writer, executor)
    finally:
        executor.close()


def rerun_turn(
    records: list[dict],
    turn_index: int,
    *,
    executor_factory=None,
    registry: Registry | None = None,
    unmatched: Literal["fail", "live"] = "fail",
    kernel_wasm: str | Path | None = None,
) -> list[RerunResult]:
    """Re-run every cell of turn N (1-based, #turn_N numbering) in
    order, sharing one kernel + one mock index so intra-turn state and
    FIFO mock queues behave as they did in the recorded run."""
    turns = turns_of(records)
    if not 1 <= turn_index <= len(turns):
        raise IndexError(f"turn {turn_index} out of range (trace has {len(turns)} turns)")
    turn = turns[turn_index - 1]
    broker, writer = _mock_broker(records, registry=registry, unmatched=unmatched)
    executor = _make_executor(broker, executor_factory, kernel_wasm)
    results: list[RerunResult] = []
    try:
        for c in turn["cells"]:
            results.append(_rerun_one(records, c["cell"], broker, writer, executor))
    finally:
        executor.close()
    return results
