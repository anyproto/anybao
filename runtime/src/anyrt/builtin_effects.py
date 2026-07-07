"""Runtime-owned nondeterminism effects (ADR-002 §4, ADR-003): the
host side of the guest shims/proxies. Recorded like any effect, so
replay is bit-exact; implementations are the ONLY place ambient
time/entropy/env are touched.
"""

from __future__ import annotations

import secrets
import time as _time
import uuid as _uuid

from .effects import Registry, effect


def register_builtin_effects(registry: Registry, *, env: dict[str, str] | None = None) -> None:
    """`env` is the explicit allowlisted mapping the harness chooses to
    expose (never raw os.environ by default — deny-by-default)."""
    env_map = env or {}

    @effect("time.now", kind="read", registry=registry)
    def time_now(ctx):
        return {"epoch": _time.time()}

    @effect("random.random", kind="read", registry=registry)
    def random_random(ctx):
        return {"value": secrets.randbits(53) / (1 << 53)}

    @effect("uuid4", kind="read", registry=registry)
    def uuid4(ctx):
        return {"hex": str(_uuid.uuid4())}

    @effect("sleep", kind="read", registry=registry)
    def sleep(ctx, seconds):
        _time.sleep(min(float(seconds), 300.0))
        return {"slept": seconds}

    @effect("env.get", kind="read", registry=registry)
    def env_get(ctx, name):
        present = name in env_map
        return {"present": present, "value": env_map.get(name)}

    @effect("batch", kind="read", registry=registry)
    def batch(ctx, name, payloads):
        # ONE guest->host crossing carrying the list; broker fans out to
        # the same single-item effect (per-item cap/normalize/redact
        # unchanged). Errors come back as {"__error": {...}} slots.
        results = ctx.call_many(name, payloads)
        from .effects import EffectError as _EE

        return {
            "results": [
                {"__error": {"type": r.type, "message": r.message}}
                if isinstance(r, _EE)
                else r
                for r in results
            ]
        }

    @effect("trace.effects_of", kind="read", registry=registry)
    def trace_effects_of(ctx, cell):
        # Compact per-cell view (ADR-003 §4): no input/output bodies.
        out = []
        for r in ctx.writer.records:
            if r["kind"] == "effect" and r.get("cell") == cell:
                out.append(
                    {
                        "seq": r["seq"],
                        "effect": r["effect"],
                        "class": r.get("meta", {}).get("class"),
                        "mocked": r.get("meta", {}).get("mocked"),
                        "error": (r.get("error") or {}).get("type"),
                    }
                )
        return {"records": out}

    @effect("trace.effect_get", kind="read", registry=registry)
    def trace_effect_get(ctx, seq):
        # One full record, blob-resolved (ADR-003 §4 / ADR-001 §2).
        for r in ctx.writer.records:
            if r["kind"] == "effect" and r["seq"] == seq:
                blobs = {**ctx.blobs, **ctx.writer.blobs}
                from . import trace as _tr

                return {
                    **r,
                    "input": _tr.resolve_blobs(r["input"], blobs),
                    "output": _tr.resolve_blobs(r["output"], blobs),
                }
        raise KeyError(f"no effect record with seq {seq}")

    @effect("kernel.boot", kind="read", registry=registry)
    def kernel_boot(ctx, **pins):
        # Input carries the determinism pins (kernel hash, hashseed,
        # schema); recording it makes them part of the run — a changed
        # kernel diverges in strict replay, which is correct.
        return None
