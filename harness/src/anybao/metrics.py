"""Metrics tuning report — the "decisions from data" checkpoint
(implementation plan, cross-cutting; ADR-003 cell metrics, ADR-005 §4
digest budget). Pure aggregation over a directory of recorded traces:
`scan` folds each run's records into per-run numbers, `report` turns
them into distributions (min/p50/p95/max, no numpy) plus `suggest` —
recommended ceilings/budgets derived from the p95s. Nothing here
executes anything; a trace directory in, a dict out.
"""

from __future__ import annotations

import math
from pathlib import Path

from .replay import load_trace
from .viewer import LLM_EFFECT

# digest sizes are approximated from tool_result content CHARS; the
# suggestion converts to tokens with the digest module's ~4 chars/token
# rule of thumb (anybao.digest.approx_tokens).
_CHARS_PER_TOKEN = 4


# ---- per-run scan ------------------------------------------------------------

def _usage_of(rec: dict) -> dict:
    usage = rec.get("meta", {}).get("usage")
    if not usage:
        out = rec.get("output")
        usage = out.get("usage") if isinstance(out, dict) else None
    return usage or {}


def scan_run(records: list[dict]) -> dict:
    """Fold one run's records into per-run numbers: cells, fuel and
    duration per cell (ADR-001 §4b cell records), effect counts by
    name, digest sizes (tool_result content lengths in llm.chat
    inputs, deduped by call_id — messages repeat across turns), and
    tokens in/out per turn (llm.chat usage)."""
    run = records[0].get("run", {}) if records and records[0].get("kind") == "header" else {}
    out = {
        "run": run.get("id"),
        "program": run.get("program"),
        "cells": 0,
        "fuel": [],
        "cell_duration_ms": [],
        "effects": {},
        "digest_bytes": [],
        "tokens_in": [],
        "tokens_out": [],
    }
    seen_results: set[str] = set()
    for r in records:
        kind = r.get("kind")
        if kind == "cell":
            out["cells"] += 1
            m = r.get("metrics") or {}
            if "fuel_used" in m:
                out["fuel"].append(m["fuel_used"])
            if "duration_ms" in m:
                out["cell_duration_ms"].append(m["duration_ms"])
        elif kind == "effect":
            name = r["effect"]
            out["effects"][name] = out["effects"].get(name, 0) + 1
            if name != LLM_EFFECT:
                continue
            usage = _usage_of(r)
            out["tokens_in"].append(usage.get("in", 0))
            out["tokens_out"].append(usage.get("out", 0))
            inp = r.get("input")
            messages = inp.get("messages") if isinstance(inp, dict) else None
            for msg in messages or []:
                for part in msg.get("parts", []) if isinstance(msg, dict) else []:
                    if part.get("type") != "tool_result":
                        continue
                    call_id = part.get("call_id") or f"@{r.get('seq')}"
                    if call_id in seen_results:
                        continue  # O(turns²) message growth: count once
                    seen_results.add(call_id)
                    out["digest_bytes"].append(len(str(part.get("content", ""))))
    return out


def scan(traces_dir: Path | str) -> list[dict]:
    """Scan every `*.jsonl` trace in a directory (blob sidecars hydrate
    via load_trace); unreadable/non-trace files are skipped — the
    report is best-effort over whatever runs are present."""
    runs = []
    for path in sorted(Path(traces_dir).glob("*.jsonl")):
        try:
            runs.append(scan_run(load_trace(path)))
        except (ValueError, KeyError, OSError):
            continue
    return runs


# ---- distributions -----------------------------------------------------------

def percentile(values: list, p: float):
    """Nearest-rank percentile over unsorted values; None when empty.
    Tiny by design — the report needs 4 numbers, not numpy."""
    if not values:
        return None
    vs = sorted(values)
    if p <= 0:
        return vs[0]
    k = min(math.ceil(p / 100 * len(vs)), len(vs))
    return vs[k - 1]


def dist(values: list) -> dict:
    return {
        "n": len(values),
        "min": percentile(values, 0),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": percentile(values, 100),
    }


# ---- the report --------------------------------------------------------------

def suggest(fuel: list, durations_ms: list, digest_bytes: list) -> dict:
    """Recommended defaults from observed p95s — the tuning knobs this
    checkpoint exists to set. Headroom rule: ceiling = 2× p95 (a limit
    should stop runaways, not typical work). Only knobs with data
    appear."""
    out: dict = {}
    p95_fuel = percentile(fuel, 95)
    if p95_fuel is not None:
        out["fuel_per_cell"] = 2 * p95_fuel                       # anyrt.wasi.WasiEngine
    p95_dur = percentile(durations_ms, 95)
    if p95_dur is not None:
        out["cell_timeout_s"] = max(1, math.ceil(2 * p95_dur / 1000))
    p95_digest = percentile(digest_bytes, 95)
    if p95_digest is not None:
        # anybao.digest.DigestPolicy.inline_token_budget: inline the
        # typical digest whole; oversize values already stub via the
        # value store, so the budget tracks the observed p95.
        out["digest_inline_token_budget"] = math.ceil(p95_digest / _CHARS_PER_TOKEN)
    return out


def report(runs: list[dict]) -> dict:
    """Aggregate scanned runs into distributions + suggested defaults."""
    fuel = [x for r in runs for x in r["fuel"]]
    durations = [x for r in runs for x in r["cell_duration_ms"]]
    digests = [x for r in runs for x in r["digest_bytes"]]
    tokens_per_turn = [
        i + o for r in runs for i, o in zip(r["tokens_in"], r["tokens_out"], strict=True)
    ]
    effects: dict[str, int] = {}
    for r in runs:
        for name, n in r["effects"].items():
            effects[name] = effects.get(name, 0) + n
    return {
        "runs": len(runs),
        "cells": sum(r["cells"] for r in runs),
        "turns": sum(len(r["tokens_in"]) for r in runs),
        "tokens_in": sum(x for r in runs for x in r["tokens_in"]),
        "tokens_out": sum(x for r in runs for x in r["tokens_out"]),
        "effects": effects,
        "fuel": dist(fuel),
        "cell_duration_ms": dist(durations),
        "digest_bytes": dist(digests),
        "tokens_per_turn": dist(tokens_per_turn),
        "suggest": suggest(fuel, durations, digests),
    }
