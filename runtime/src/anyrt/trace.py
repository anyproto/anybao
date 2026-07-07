"""Trace v2 — ADR-001: append-only JSONL log; replay = derived views.

Record kinds: header (line 1), effect, cell. M0 scope: no blob spill
(threshold effectively infinite — M1), no traceDiff view yet.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 2


def canonical_json(value: Any) -> str:
    """Canonical form: sorted keys, no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def input_key(effect: str, canonical_input: Any) -> str:
    # \x00 is a hash-domain separator only — it lives in the local
    # hashlib buffer and is never serialized/stored (json.dumps always
    # escapes control chars, so canonical JSON can't carry raw NULs
    # either). NB the known anyenc/fastjson NUL issue applies to
    # strings written into any-store DATASET RECORDS — that guard
    # belongs at the anyclient write boundary, not here.
    h = hashlib.sha256()
    h.update(effect.encode())
    h.update(b"\x00")
    h.update(canonical_json(canonical_input).encode())
    return "sha256:" + h.hexdigest()


class DivergenceError(Exception):
    """Strict replay only: the next call does not match the next record.
    The hard error IS the feature (determinism made testable); loose
    mock mode never raises this — ADR-001 §5."""

    def __init__(self, expected: dict | None, actual: dict):
        self.expected = expected
        self.actual = actual
        exp = (
            f"{expected['effect']} key={expected['key'][:24]}…"
            if expected and expected.get("kind") == "effect"
            else repr(expected and expected.get("kind"))
        )
        act = f"{actual.get('effect', actual.get('kind'))} key={actual.get('key', '')[:24]}…"
        super().__init__(f"replay divergence: expected {exp}, got {act}")


@dataclass
class TraceWriter:
    """Appends records in execution order, assigns seq."""

    run: dict[str, Any]
    records: list[dict] = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.records.append({"kind": "header", "schema": SCHEMA, "run": self.run})

    def next_seq(self) -> int:
        # seq = the record's stable address (refs, views, lookups) —
        # replay never reads it; ADR-001 §2 amendment.
        self._seq += 1
        return self._seq

    def effect(
        self,
        *,
        effect: str,
        cell: str | None,
        input: Any,
        key: str,
        output: Any = None,
        error: dict | None = None,
        meta: dict | None = None,
    ) -> dict:
        rec = {
            "kind": "effect",
            "seq": self.next_seq(),
            "effect": effect,
            "cell": cell,
            "input": input,
            "key": key,
            "output": output,
            "error": error,
            "meta": meta or {},
        }
        self.records.append(rec)
        return rec

    def cell(
        self,
        *,
        cell: str,
        ok: bool,
        error: dict | None = None,
        interrupted: bool = False,
        metrics: dict | None = None,
    ) -> dict:
        rec = {
            "kind": "cell",
            "seq": self.next_seq(),
            "cell": cell,
            "ok": ok,
            "error": error,
            "interrupted": interrupted,
            "metrics": metrics or {},
        }
        self.records.append(rec)
        return rec

    def dump(self, path: Path) -> None:
        path.write_text("".join(canonical_json(r) + "\n" for r in self.records))


def load(path: Path) -> list[dict]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not records or records[0].get("kind") != "header":
        raise ValueError(f"not a trace file (missing header): {path}")
    if records[0].get("schema") != SCHEMA:
        raise ValueError(f"trace schema {records[0].get('schema')} != {SCHEMA}")
    return records


class ReplayCursor:
    """Strict replay: every effect call must match the next unconsumed
    effect record; cell records are checkpoints (cell id + ok matched,
    metrics ignored — they legitimately vary between runs)."""

    def __init__(self, records: list[dict]):
        self._records = [r for r in records if r["kind"] in ("effect", "cell")]
        self._pos = 0

    def _peek(self) -> dict | None:
        return self._records[self._pos] if self._pos < len(self._records) else None

    def expect_effect(self, effect: str, key: str) -> dict:
        rec = self._peek()
        actual = {"kind": "effect", "effect": effect, "key": key}
        if rec is None or rec["kind"] != "effect" or rec["effect"] != effect or rec["key"] != key:
            raise DivergenceError(rec, actual)
        self._pos += 1
        return rec

    def expect_cell(self, cell: str, ok: bool) -> dict:
        rec = self._peek()
        if rec is None or rec["kind"] != "cell" or rec["cell"] != cell or rec["ok"] != ok:
            raise DivergenceError(rec, {"kind": "cell", "cell": cell, "ok": ok})
        self._pos += 1
        return rec

    def exhausted(self) -> bool:
        return self._pos >= len(self._records)


class MockIndex:
    """Loose mock mode: (effect, key) -> FIFO output queue (queue order =
    log order — v1 pop semantics on sturdier keys). Unmatched policy is
    the caller's ('fail' under tests, 'live' interactively)."""

    def __init__(self, records: list[dict]):
        self._queues: dict[tuple[str, str], list[dict]] = {}
        for r in records:
            if r["kind"] == "effect":
                self._queues.setdefault((r["effect"], r["key"]), []).append(r)

    def pop(self, effect: str, key: str) -> dict | None:
        q = self._queues.get((effect, key))
        return q.pop(0) if q else None
