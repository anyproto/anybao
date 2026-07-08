"""Trace viewer — ADR-006 § traceRef: the human-facing debug log is a
VIEW over the trace (ADR-001), not a second data format. Pure renderer:
(records, blobs) -> str, no I/O in the core. `#turn_N` anchors resolve
to the Nth `llm.chat` effect record (`turn_anchor`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anyrt import trace as tr

LLM_EFFECT = "llm.chat"
_PREVIEW_LIMIT = 120


# ---- view model (derived from the log, ADR-001 §1) --------------------------

@dataclass
class CellRun:
    """One cell's slice of a turn: its code (from the run_cell tool_call
    args), the effects attributed to it, and its terminal cell record."""

    cell_id: str
    code: str | None = None
    effects: list[dict] = field(default_factory=list)
    end: dict | None = None


@dataclass
class TurnView:
    n: int                                          # 1-based, the #turn_N anchor
    record: dict                                    # the llm.chat effect record
    cells: list[CellRun] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)   # non-cell effects in this turn


@dataclass
class TraceView:
    header: dict | None
    preamble: list[dict]                            # records before the first turn
    turns: list[TurnView]


def _is_blob_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"__blob", "bytes"}


def _deref(value: Any, blobs: dict[str, str]) -> Any:
    """resolve_blobs, but tolerant of a missing sidecar entry — the
    viewer degrades to the '[N bytes]' note instead of raising."""
    if _is_blob_ref(value) and value["__blob"] in blobs:
        return tr.resolve_blobs(value, blobs)
    return value


def _attributed_cell(r: dict) -> str | None:
    return r.get("cell") or r.get("meta", {}).get("cell")


def build_view(records: list[dict], blobs: dict[str, str] | None = None) -> TraceView:
    blobs = blobs or {}
    header = records[0] if records and records[0].get("kind") == "header" else None
    view = TraceView(header=header, preamble=[], turns=[])
    cells: dict[str, CellRun] = {}
    cur: TurnView | None = None

    def cell_run(cell_id: str) -> CellRun:
        run = cells.get(cell_id)
        if run is None:
            # effect for a cell no tool_call announced (partial log) —
            # keep it visible under the current turn rather than dropping it
            run = cells[cell_id] = CellRun(cell_id=cell_id)
            if cur is not None:
                cur.cells.append(run)
        return run

    for r in records:
        kind = r.get("kind")
        if kind == "effect" and r.get("effect") == LLM_EFFECT:
            cur = TurnView(n=len(view.turns) + 1, record=r)
            view.turns.append(cur)
            output = _deref(r.get("output"), blobs)
            for part in (output or {}).get("parts", []) if isinstance(output, dict) else []:
                if part.get("type") == "tool_call" and part.get("name") == "run_cell":
                    run = CellRun(cell_id=part["id"], code=part.get("args", {}).get("code"))
                    cells[part["id"]] = run
                    cur.cells.append(run)
        elif kind == "effect":
            cell_id = _attributed_cell(r)
            if cell_id is not None:
                cell_run(cell_id).effects.append(r)
            elif cur is not None:
                cur.effects.append(r)
            else:
                view.preamble.append(r)
        elif kind == "cell":
            cell_run(r["cell"]).end = r
    return view


# ---- rendering ---------------------------------------------------------------

def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    if _is_blob_ref(value):
        return f"[{value['bytes']} bytes]"          # spilled — never inlined
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _effect_line(r: dict) -> str:
    meta = r.get("meta", {})
    tags = [
        t for t in (
            meta.get("class"),
            "mocked" if meta.get("mocked") else None,
            f"{meta['durMs']}ms" if "durMs" in meta else None,
        ) if t
    ]
    line = f"#{r['seq']} {r['effect']}"
    if tags:
        line += " [" + ", ".join(tags) + "]"
    err = r.get("error")
    if err:
        line += f" !! {err.get('type', 'error')}: {err.get('message', '')}"
    else:
        line += " -> " + _preview(r.get("output"))
    return line


def _cell_result_line(end: dict | None) -> str:
    if end is None:
        return "result: (no cell record)"
    if end.get("interrupted"):
        status = "interrupted"
    elif end.get("ok"):
        status = "ok"
    else:
        err = end.get("error") or {}
        status = f"error {err.get('type', '?')}: {err.get('message', '')}"
    m = end.get("metrics", {})
    metrics = [
        f"fuel={m['fuel_used']}" if "fuel_used" in m else None,
        f"mem_pages={m['mem_pages']}" if "mem_pages" in m else None,
        f"duration={m['duration_ms']}ms" if "duration_ms" in m else None,
    ]
    line = f"result: {status}"
    if any(metrics):
        line += "  " + " ".join(x for x in metrics if x)
    return line


def _turn_lines(turn: TurnView, blobs: dict[str, str]) -> list[str]:
    r = turn.record
    usage = r.get("meta", {}).get("usage") or {}
    head = f"turn {turn.n} (seq {r['seq']})"
    if usage:
        head += f"  tokens in={usage.get('in', '?')} out={usage.get('out', '?')}"
    lines = [head]

    output = _deref(r.get("output"), blobs)
    if _is_blob_ref(output):
        lines.append(f"  assistant: [output spilled, {output['bytes']} bytes]")
    elif r.get("error"):
        err = r["error"]
        lines.append(f"  !! {err.get('type', 'error')}: {err.get('message', '')}")
    elif isinstance(output, dict):
        parts = output.get("parts", [])
        thinking = sum(1 for p in parts if p.get("type") == "thinking")
        if thinking:
            lines.append(f"  (thinking ×{thinking})")
        for p in parts:
            if p.get("type") == "text":
                for tl in p["text"].splitlines() or [""]:
                    lines.append(f"  assistant: {tl}")
            elif p.get("type") == "tool_call":
                lines.append(f"  tool_call {p.get('name', '?')} ({p.get('id', '?')})")

    for run in turn.cells:
        lines.append(f"  cell {run.cell_id}:")
        if run.code is not None:
            lines.append("    code:")
            lines.extend(f"      {cl}" for cl in run.code.splitlines() or [""])
        if run.effects:
            lines.append("    effects:")
            lines.extend(f"      {_effect_line(e)}" for e in run.effects)
        lines.append(f"    {_cell_result_line(run.end)}")

    if turn.effects:
        lines.append("  effects:")
        lines.extend(f"    {_effect_line(e)}" for e in turn.effects)
    return lines


def render_trace(records: list[dict], blobs: dict[str, str] | None = None) -> str:
    """Readable timeline over a trace log: header, then one block per
    turn (assistant output, cells with code/effects/metrics, grouped
    non-cell effects). Pure function of (records, blobs)."""
    blobs = blobs or {}
    view = build_view(records, blobs)
    lines: list[str] = []

    if view.header is not None:
        run = view.header.get("run", {})
        head = f"run {run.get('id', '?')}"
        if run.get("program"):
            head += f" — {run['program']}"
        lines += [head, ""]

    if view.preamble:
        lines.append("before turn 1:")
        lines.extend(f"  {_effect_line(e)}" for e in view.preamble)
        lines.append("")

    for turn in view.turns:
        lines.extend(_turn_lines(turn, blobs))
        lines.append("")

    n_effects = sum(1 for r in records if r.get("kind") == "effect")
    tok_in = tok_out = 0
    for t in view.turns:
        usage = t.record.get("meta", {}).get("usage") or {}
        tok_in += usage.get("in", 0)
        tok_out += usage.get("out", 0)
    lines.append(
        f"totals: {len(view.turns)} turns, {n_effects} effects, "
        f"tokens in={tok_in} out={tok_out}"
    )
    return "\n".join(lines) + "\n"


def turn_anchor(records: list[dict], n: int) -> dict:
    """Resolve a `#turn_N` anchor (1-based) to the Nth llm.chat effect
    record — ADR-006 § traceRef. Raises IndexError when out of range."""
    turns = [
        r for r in records
        if r.get("kind") == "effect" and r.get("effect") == LLM_EFFECT
    ]
    if not 1 <= n <= len(turns):
        raise IndexError(f"turn {n} out of range (trace has {len(turns)} turns)")
    return turns[n - 1]


def render_file(path: Path | str) -> str:
    """Convenience: load a trace file + its blob sidecar and render."""
    p = Path(path)
    return render_trace(tr.load(p), tr.load_blobs(p))
