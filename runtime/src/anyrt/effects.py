"""Effect boundary — ADR-002: one broker pipeline for every call.

Pipeline (ADR-002 §2): normalize → key → capability check →
replay/mock consult → execute → record. The capability RULE lives
here — an optional `grants` object is consulted before anything runs;
denials are recorded (`error.type = "capability_denied"`) and raised.
No `grants` = the permissive default profile (mechanism enabled,
policy wide open). Grant POLICY lives harness-side (anybao.caps).
Redaction applies to declared dotted paths.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from . import trace as tr


class EffectError(Exception):
    def __init__(self, effect: str, type_: str, message: str, seq: int | None = None):
        self.effect = effect
        self.type = type_
        self.message = message
        self.seq = seq
        super().__init__(f"{effect}: {type_}: {message}")


@dataclass(frozen=True)
class EffectDef:
    name: str
    fn: Callable[..., Any]
    kind: Literal["read", "mutate"]
    normalize: Callable[[dict], dict] | None
    redact: tuple[str, ...]
    cap: str


class Registry:
    def __init__(self) -> None:
        self._defs: dict[str, EffectDef] = {}

    def add(self, d: EffectDef) -> None:
        if d.name in self._defs:
            raise ValueError(f"effect already registered: {d.name}")
        self._defs[d.name] = d

    def get(self, name: str) -> EffectDef:
        if name not in self._defs:
            raise EffectError(name, "unknown_effect", f"no such effect: {name}")
        return self._defs[name]


def effect(
    name: str,
    *,
    kind: Literal["read", "mutate"],
    registry: Registry,
    normalize: Callable[[dict], dict] | None = None,
    redact: tuple[str, ...] = (),
    cap: str | None = None,
):
    """Declare a host-side effect (ADR-002 §1). `fn(ctx, **payload)`."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        registry.add(
            EffectDef(
                name=name,
                fn=fn,
                kind=kind,
                normalize=normalize,
                redact=tuple(redact),
                cap=cap or name,
            )
        )
        return fn

    return deco


def _redact(payload: dict, paths: tuple[str, ...]) -> dict:
    if not paths:
        return payload
    import copy

    out = copy.deepcopy(payload)
    for path in paths:
        node = out
        parts = path.split(".")
        for p in parts[:-1]:
            if not isinstance(node, dict) or p not in node:
                node = None
                break
            node = node[p]
        if isinstance(node, dict) and parts[-1] in node:
            node[parts[-1]] = "<redacted>"
    return out


Mode = Literal["record", "replay", "mock"]


class Grants(Protocol):
    """The broker's view of a grant set — policy objects (anybao.caps)
    satisfy this; the broker only ever asks yes/no."""

    def allowed(self, cap: str) -> bool: ...


class Broker:
    """The one pipeline. `ctx` handed to effect impls is the broker
    itself — credential/config resolution happens inside the boundary."""

    def __init__(
        self,
        registry: Registry,
        writer: tr.TraceWriter,
        *,
        mode: Mode = "record",
        cursor: tr.ReplayCursor | None = None,
        mock_index: tr.MockIndex | None = None,
        mock_unmatched: Literal["fail", "live"] = "fail",
        blobs: dict[str, str] | None = None,
        grants: Grants | None = None,
    ):
        self.registry = registry
        self.writer = writer
        self.mode = mode
        self.cursor = cursor
        self.mock_index = mock_index
        self.mock_unmatched = mock_unmatched
        self.grants = grants  # None = permissive default profile (ADR-002 §2)
        self.blobs = blobs or {}  # sidecar of the trace being replayed
        self.current_cell: str | None = None
        self._span_stack: list[dict] = []  # open spans, innermost last
        self._span_n = 0

    def _resolve(self, value):
        return tr.resolve_blobs(value, self.blobs)

    def call_many(self, name: str, payloads: list[dict]) -> list:
        """Batch fan-out (ADR-002 resolved Q3): N individual records in
        INPUT order regardless of completion order (strict replay stays
        deterministic). Each item marked meta.batch={id,i}. Per-item
        failure is an EffectError VALUE in that slot, not a raised
        exception. M1: sequential host execution (concurrency is a
        later optimization; the record contract is what matters)."""
        results: list = []
        for i, payload in enumerate(payloads):
            try:
                results.append(self.call(name, payload, _batch=(id(payloads), i)))
            except EffectError as e:
                results.append(e)
        return results

    def span_begin(self, name: str, input: Any = None) -> str:
        """Open a guest-declared span (ADR-001 §4c): subsequent effect
        records are stamped with its id until span_end. Returns the id."""
        import time as _time

        self._span_n += 1
        sid = f"s{self._span_n}"  # execution order => deterministic ids
        canonical = input if input is not None else {}
        key = tr.input_key(name, canonical)
        if self.mode == "replay":
            assert self.cursor is not None
            self.cursor.expect_span_begin(name, key)
        parent = self._span_stack[-1]["id"] if self._span_stack else None
        self.writer.span_begin(
            span=sid, name=name, cell=self.current_cell,
            input=canonical, key=key, parent=parent,
        )
        self._span_stack.append(
            {"id": sid, "name": name, "t0": _time.monotonic(),
             "effects": 0, "mutations": 0}
        )
        return sid

    def span_end(self, *, ok: bool, output: Any = None, error: dict | None = None) -> None:
        """Close the innermost open span."""
        if not self._span_stack:
            raise EffectError("span.end", "no_open_span", "span.end without span.begin")
        self._close_span(ok=ok, output=output, error=error)

    def _close_span(self, *, ok: bool, output: Any = None, error: dict | None = None) -> None:
        import time as _time

        top = self._span_stack.pop()
        if self.mode == "replay":
            assert self.cursor is not None
            self.cursor.expect_span_end(top["name"], ok)
        self.writer.span_end(
            span=top["id"], name=top["name"], cell=self.current_cell,
            ok=ok, output=output, error=error,
            meta={
                "durMs": int((_time.monotonic() - top["t0"]) * 1000),
                "effects": top["effects"], "mutations": top["mutations"],
            },
        )

    def cell_done(
        self,
        *,
        cell: str,
        ok: bool,
        error: dict | None = None,
        interrupted: bool = False,
        metrics: dict | None = None,
    ) -> None:
        """Cell lifecycle record (ADR-001 §4b). In strict replay the
        matching cell checkpoint is consumed (cell id + ok compared;
        metrics legitimately vary run-to-run)."""
        while self._span_stack:
            # a trapped cell skips guest finally — force-close so the
            # log stays well-nested (ADR-001 §4c)
            self._close_span(
                ok=False,
                error={"type": "unclosed_span",
                       "message": f"cell {cell} ended with span open"},
            )
        if self.mode == "replay":
            assert self.cursor is not None
            self.cursor.expect_cell(cell, ok)
        self.writer.cell(
            cell=cell, ok=ok, error=error, interrupted=interrupted, metrics=metrics
        )

    def call(self, name: str, payload: dict, _batch: tuple | None = None) -> Any:
        import time as _time

        batch_meta = {"batch": {"id": _batch[0], "i": _batch[1]}} if _batch else {}
        d = self.registry.get(name)
        canonical = d.normalize(payload) if d.normalize else payload
        canonical = _redact(canonical, d.redact)
        key = tr.input_key(name, canonical)
        span = self._span_stack[-1]["id"] if self._span_stack else None

        def _bump():  # span meta counters: one bump per record written
            for s in self._span_stack:
                s["effects"] += 1
                if d.kind == "mutate":
                    s["mutations"] += 1

        # Capability check precedes replay/mock consult AND execute
        # (ADR-002 §2): a denial is a recorded fact, never a silent gap.
        if self.grants is not None and not self.grants.allowed(d.cap):
            err = {"type": "capability_denied",
                   "message": f"capability not granted: {d.cap} (effect {name})"}
            _bump()
            rec = self.writer.effect(
                effect=name, cell=self.current_cell, input=canonical, key=key,
                output=None, error=err,
                meta={"mocked": False, "class": d.kind, **batch_meta}, span=span,
            )
            raise EffectError(name, err["type"], err["message"], seq=rec["seq"])

        if self.mode == "replay":
            assert self.cursor is not None
            rec = self.cursor.expect_effect(name, key)
            _bump()
            self.writer.effect(
                effect=name, cell=self.current_cell, input=canonical, key=key,
                output=rec["output"], error=rec["error"],
                meta={**rec.get("meta", {}), "mocked": True, "class": d.kind, **batch_meta},
                span=span,
            )
            if rec["error"]:
                raise EffectError(name, rec["error"]["type"], rec["error"]["message"])
            return self._resolve(rec["output"])

        if self.mode == "mock":
            assert self.mock_index is not None
            rec = self.mock_index.pop(name, key)
            if rec is not None:
                _bump()
                self.writer.effect(
                    effect=name, cell=self.current_cell, input=canonical, key=key,
                    output=rec["output"], error=rec["error"],
                    meta={"mocked": True, "class": d.kind, **batch_meta}, span=span,
                )
                if rec["error"]:
                    raise EffectError(name, rec["error"]["type"], rec["error"]["message"])
                return self._resolve(rec["output"])
            if self.mock_unmatched == "fail":
                raise EffectError(name, "unmatched_mock", f"no recorded output for {name} {key}")
            # fall through: live execution (traceDiff view = mocked: false)

        t0 = _time.monotonic()
        try:
            output = d.fn(self, **payload)
            err = None
        except EffectError:
            raise
        except Exception as e:  # effect failure is data + typed exception
            output = None
            err = {"type": type(e).__name__, "message": str(e)}
        _bump()
        rec = self.writer.effect(
            effect=name, cell=self.current_cell, input=canonical, key=key,
            output=output, error=err,
            meta={
                "durMs": int((_time.monotonic() - t0) * 1000),
                "mocked": False, "class": d.kind, **batch_meta,
            },
            span=span,
        )
        if err:
            raise EffectError(name, err["type"], err["message"], seq=rec["seq"])
        return output
