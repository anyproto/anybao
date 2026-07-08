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
        elif kind in ("effect", "span"):
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


def _group_spans(recs: list[dict]) -> list[tuple]:
    """Group a flat record list into render items: ("effect", rec) and
    ("span", begin, end|None, inner) — inner covers everything between
    the pair, nested spans included (ADR-001 §4c)."""
    items: list[tuple] = []
    i = 0
    while i < len(recs):
        r = recs[i]
        if r.get("kind") == "span" and r.get("phase") == "begin":
            sid, inner, end = r["span"], [], None
            j = i + 1
            while j < len(recs):
                x = recs[j]
                if x.get("kind") == "span" and x.get("span") == sid and x.get("phase") == "end":
                    end = x
                    break
                inner.append(x)
                j += 1
            items.append(("span", r, end, inner))
            i = j + 1
        elif r.get("kind") == "span":
            i += 1  # stray end (partial log) — its begin already grouped it
        else:
            items.append(("effect", r))
            i += 1
    return items


def _span_line(begin: dict, end: dict | None, inner: list[dict]) -> str:
    n = (end or {}).get("meta", {}).get("effects")
    if n is None:
        n = sum(1 for x in inner if x.get("kind") == "effect")
    tags = ["span", f"{n} effect" + ("s" if n != 1 else "")]
    if end and "durMs" in end.get("meta", {}):
        tags.append(f"{end['meta']['durMs']}ms")
    line = f"#{begin['seq']} {begin['name']} [" + ", ".join(tags) + "]"
    if end is None:
        return line + " (unclosed)"
    err = end.get("error")
    if not end.get("ok") and err:
        return line + f" !! {err.get('type', 'error')}: {err.get('message', '')}"
    return line + " -> " + _preview(end.get("output"))


def _span_mutates(end: dict | None, inner: list[dict]) -> bool:
    m = (end or {}).get("meta", {}).get("mutations")
    if m is None:
        m = sum(
            1 for x in inner
            if x.get("kind") == "effect" and x.get("meta", {}).get("class") == "mutate"
        )
    return m > 0


def _record_lines(recs: list[dict], *, marked: bool = False,
                  expand_spans: bool = False) -> list[str]:
    """Effect/span one-liners, spans collapsed unless expand_spans
    (then: span line + inner records indented beneath it)."""
    eff = _mark_effect_line if marked else _effect_line
    out: list[str] = []
    for item in _group_spans(recs):
        if item[0] == "effect":
            out.append(eff(item[1]))
            continue
        _, begin, end, inner = item
        line = _span_line(begin, end, inner)
        if marked:
            line = ("*" if _span_mutates(end, inner) else " ") + " " + line
        out.append(line)
        if expand_spans:
            out.extend(
                "  " + ln
                for ln in _record_lines(inner, marked=marked, expand_spans=True)
            )
    return out


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


def _turn_lines(turn: TurnView, blobs: dict[str, str],
                expand_spans: bool = False) -> list[str]:
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
            lines.extend(
                f"      {ln}"
                for ln in _record_lines(run.effects, expand_spans=expand_spans)
            )
        lines.append(f"    {_cell_result_line(run.end)}")

    if turn.effects:
        lines.append("  effects:")
        lines.extend(
            f"    {ln}" for ln in _record_lines(turn.effects, expand_spans=expand_spans)
        )
    return lines


def render_trace(records: list[dict], blobs: dict[str, str] | None = None,
                 *, expand_spans: bool = False) -> str:
    """Readable timeline over a trace log: header, then one block per
    turn (assistant output, cells with code/effects/metrics, grouped
    non-cell effects). Spans render collapsed unless expand_spans.
    Pure function of (records, blobs)."""
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
        lines.extend(
            f"  {ln}" for ln in _record_lines(view.preamble, expand_spans=expand_spans)
        )
        lines.append("")

    for turn in view.turns:
        lines.extend(_turn_lines(turn, blobs, expand_spans))
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


# ---- run render — the trace viewer proper (M6, ADR-006 §1) -------------------
#
# render_trace above is the minimal timeline; render_run is the complete
# human-side view of one run: summary header, then a `#turn_N` block per
# turn (the anchor form traceRefs use) with the user text, assistant
# output, each cell's code + effects + result + the digest the model
# saw, mutations marked, and the llm usage/cost line.

_DIGEST_LINES = 8


def _usage_of(rec: dict) -> dict:
    usage = rec.get("meta", {}).get("usage")
    if not usage:
        out = rec.get("output")
        usage = out.get("usage") if isinstance(out, dict) else None
    return usage or {}


def _turn_messages(rec: dict, blobs: dict[str, str]) -> list | None:
    """The llm.chat input messages, or None when spilled and the
    sidecar is missing (degrade, never raise)."""
    inp = _deref(rec.get("input"), blobs)
    if isinstance(inp, dict) and isinstance(inp.get("messages"), list):
        return inp["messages"]
    return None


def _delta_messages(turns: list[TurnView], i: int, blobs: dict[str, str]) -> list:
    """Messages NEW in turn i's input relative to turn i-1's — each
    llm.chat records the full array (ADR-001 resolved Q1), so the delta
    is a suffix slice."""
    msgs = _turn_messages(turns[i].record, blobs)
    if msgs is None:
        return []
    prev = _turn_messages(turns[i - 1].record, blobs) if i else []
    if prev is None:
        return []
    return msgs[len(prev):]


def _user_texts(delta: list, first_turn: bool) -> list[str]:
    """User-visible text in a delta. Turn 1's delta is the whole boot
    window + the actual user message — take only the LAST text-bearing
    user message there; later deltas are injections (all shown)."""
    by_msg = []
    for m in delta:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        texts = [p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text"]
        if texts:
            by_msg.append(texts)
    if first_turn:
        by_msg = by_msg[-1:]
    return [t for texts in by_msg for t in texts]


def _digest_map(turns: list[TurnView], blobs: dict[str, str]) -> dict[str, str]:
    """call_id -> tool_result content from the llm.chat inputs — the
    digest the model actually saw for each cell (ADR-005 §4)."""
    out: dict[str, str] = {}
    for t in turns:
        for m in _turn_messages(t.record, blobs) or []:
            if not isinstance(m, dict):
                continue
            for p in m.get("parts", []):
                if p.get("type") == "tool_result" and p.get("call_id") not in out:
                    out[p["call_id"]] = str(p.get("content", ""))
    return out


def _mark_effect_line(r: dict) -> str:
    """Effect one-liner with mutations highlighted: `*` in the gutter."""
    mark = "*" if r.get("meta", {}).get("class") == "mutate" else " "
    return f"{mark} {_effect_line(r)}"


def _llm_line(rec: dict) -> str:
    usage = _usage_of(rec)
    meta = rec.get("meta", {})
    line = f"llm: tokens in={usage.get('in', '?')} out={usage.get('out', '?')}"
    cost = usage.get("costUsd", meta.get("costUsd"))
    if cost is not None:
        line += f" cost=${cost:.4f}"
    if "durMs" in meta:
        line += f" ({meta['durMs']}ms)"
    return line


def _clip_lines(text: str, limit: int = _DIGEST_LINES) -> list[str]:
    lines = text.splitlines() or [""]
    out = [
        ln if len(ln) <= _PREVIEW_LIMIT else ln[: _PREVIEW_LIMIT - 1] + "…"
        for ln in lines[:limit]
    ]
    if len(lines) > limit:
        out.append(f"… ({len(lines) - limit} more lines)")
    return out


def _run_totals(records: list[dict]) -> dict:
    t = {"turns": 0, "cells": 0, "effects": 0, "mutations": 0,
         "tok_in": 0, "tok_out": 0, "errors": 0}
    for r in records:
        kind = r.get("kind")
        if kind == "effect":
            t["effects"] += 1
            if r.get("meta", {}).get("class") == "mutate":
                t["mutations"] += 1
            if r.get("error"):
                t["errors"] += 1
            if r.get("effect") == LLM_EFFECT:
                t["turns"] += 1
                usage = _usage_of(r)
                t["tok_in"] += usage.get("in", 0)
                t["tok_out"] += usage.get("out", 0)
        elif kind == "cell":
            t["cells"] += 1
            if not r.get("ok"):
                t["errors"] += 1
    return t


def _totals_str(t: dict) -> str:
    return (
        f"{t['turns']} turns, {t['cells']} cells, "
        f"{t['effects']} effects ({t['mutations']} mutate), "
        f"tokens in={t['tok_in']} out={t['tok_out']}"
    )


def _run_head(records: list[dict]) -> str:
    run = records[0].get("run", {}) if records and records[0].get("kind") == "header" else {}
    head = f"run {run.get('id', '?')}"
    if run.get("program"):
        head += f" — {run['program']}"
    if run.get("chatId"):
        head += f"  chat={run['chatId']}"
    if run.get("startedAt") is not None:
        head += f"  started={run['startedAt']}"
    return head


def _run_turn_lines(
    turn: TurnView, user_texts: list[str], digests: dict[str, str],
    blobs: dict[str, str], expand_spans: bool = False,
) -> list[str]:
    r = turn.record
    lines = [f"#turn_{turn.n} (seq {r['seq']})"]
    for t in user_texts:
        lines.extend(f"  user: {tl}" for tl in t.splitlines() or [""])

    output = _deref(r.get("output"), blobs)
    if _is_blob_ref(output):
        lines.append(f"  assistant: [output spilled, {output['bytes']} bytes]")
    elif r.get("error"):
        err = r["error"]
        lines.append(f"  !! llm {err.get('type', 'error')}: {err.get('message', '')}")
    elif isinstance(output, dict):
        for p in output.get("parts", []):
            if p.get("type") == "text":
                lines.extend(f"  assistant: {tl}" for tl in p["text"].splitlines() or [""])
            elif p.get("type") == "tool_call":
                lines.append(f"  tool_call {p.get('name', '?')} ({p.get('id', '?')})")

    for run in turn.cells:
        lines.append(f"  cell {run.cell_id}:")
        if run.code is not None:
            lines.append("    code:")
            lines.extend(f"      {cl}" for cl in run.code.splitlines() or [""])
        if run.effects:
            lines.append("    effects:")
            lines.extend(
                f"    {ln}"
                for ln in _record_lines(run.effects, marked=True,
                                        expand_spans=expand_spans)
            )
        lines.append(f"    {_cell_result_line(run.end)}")
        if run.cell_id in digests:
            lines.append("    digest:")
            lines.extend(f"      {dl}" for dl in _clip_lines(digests[run.cell_id]))

    if turn.effects:
        lines.append("  effects:")
        lines.extend(
            f"  {ln}"
            for ln in _record_lines(turn.effects, marked=True,
                                    expand_spans=expand_spans)
        )
    lines.append(f"  {_llm_line(r)}")
    return lines


def render_run(records: list[dict], blobs: dict[str, str] | None = None,
               *, expand_spans: bool = False) -> str:
    """The complete human-side render of one run: summary header (run
    id, program, chatId, totals), then turn-by-turn `#turn_N` blocks —
    user text, assistant text/tool calls, per-cell code + effect
    one-liners (mutations marked `*`, spans collapsed unless
    expand_spans) + result + the digest the model saw, and the llm
    usage/cost line. Plain text, no ANSI."""
    blobs = blobs or {}
    view = build_view(records, blobs)
    digests = _digest_map(view.turns, blobs)
    lines = [_run_head(records), f"totals: {_totals_str(_run_totals(records))}", ""]

    if view.preamble:
        lines.append("before turn 1:")
        lines.extend(
            f"  {ln}"
            for ln in _record_lines(view.preamble, marked=True,
                                    expand_spans=expand_spans)
        )
        lines.append("")

    for i, turn in enumerate(view.turns):
        texts = _user_texts(_delta_messages(view.turns, i, blobs), first_turn=i == 0)
        lines.extend(_run_turn_lines(turn, texts, digests, blobs, expand_spans))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_run_summary(records: list[dict]) -> str:
    """One line per run — the trigger-run-list form: id, program,
    chat, totals, ok/error."""
    t = _run_totals(records)
    status = "error" if t["errors"] else "ok"
    return f"{_run_head(records)}: {_totals_str(t)} — {status}"


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
