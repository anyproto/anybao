"""Effect boundary — ADR-002: one broker pipeline for every call.

M0 scope: normalize → key → replay/mock consult → execute → record.
Capability checks are permissive bookkeeping (cap recorded on the
registration, enforcement wiring lands M1); redaction applies to
declared dotted paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

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
    ):
        self.registry = registry
        self.writer = writer
        self.mode = mode
        self.cursor = cursor
        self.mock_index = mock_index
        self.mock_unmatched = mock_unmatched
        self.current_cell: str | None = None

    def call(self, name: str, payload: dict) -> Any:
        import time as _time

        d = self.registry.get(name)
        canonical = d.normalize(payload) if d.normalize else payload
        canonical = _redact(canonical, d.redact)
        key = tr.input_key(name, canonical)

        if self.mode == "replay":
            assert self.cursor is not None
            rec = self.cursor.expect_effect(name, key)
            self.writer.effect(
                effect=name, cell=self.current_cell, input=canonical, key=key,
                output=rec["output"], error=rec["error"],
                meta={**rec.get("meta", {}), "mocked": True, "class": d.kind},
            )
            if rec["error"]:
                raise EffectError(name, rec["error"]["type"], rec["error"]["message"])
            return rec["output"]

        if self.mode == "mock":
            assert self.mock_index is not None
            rec = self.mock_index.pop(name, key)
            if rec is not None:
                self.writer.effect(
                    effect=name, cell=self.current_cell, input=canonical, key=key,
                    output=rec["output"], error=rec["error"],
                    meta={"mocked": True, "class": d.kind},
                )
                if rec["error"]:
                    raise EffectError(name, rec["error"]["type"], rec["error"]["message"])
                return rec["output"]
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
        rec = self.writer.effect(
            effect=name, cell=self.current_cell, input=canonical, key=key,
            output=output, error=err,
            meta={"durMs": int((_time.monotonic() - t0) * 1000), "mocked": False, "class": d.kind},
        )
        if err:
            raise EffectError(name, err["type"], err["message"], seq=rec["seq"])
        return output
