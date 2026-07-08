"""Runtime-owned nondeterminism effects (ADR-002 §4, ADR-003): the
host side of the guest shims/proxies. Recorded like any effect, so
replay is bit-exact; implementations are the ONLY place ambient
time/entropy/env are touched.
"""

from __future__ import annotations

import secrets
import time as _time
import uuid as _uuid
from dataclasses import dataclass

from .effects import Registry, effect


@dataclass
class Resolved:
    space_id: str
    object_id: str
    marker: int       # _ver/_addSeq — probe-cache key (ADR-004 §4)
    source_hash: str
    source: str


class ModuleResolver:
    def __call__(self, spec: str, frm: str | None):  # -> Resolved
        raise NotImplementedError


class DictResolver(ModuleResolver):
    """M1/test resolver: {spec: source} with synthetic markers."""

    def __init__(self, sources: dict[str, str]):
        import hashlib as _h

        self._sources = sources
        self._h = _h

    def __call__(self, spec, frm):
        if spec not in self._sources:
            raise KeyError(f"program not found: {spec}")
        src = self._sources[spec]
        return Resolved(
            space_id="dict",
            object_id=spec,
            marker=1,
            source_hash="sha256:" + self._h.sha256(src.encode()).hexdigest(),
            source=src,
        )


def register_builtin_effects(
    registry: Registry,
    *,
    env: dict[str, str] | None = None,
    resolver: ModuleResolver | None = None,
) -> None:
    """`env` is the explicit allowlisted mapping the harness chooses to
    expose (never raw os.environ by default — deny-by-default).
    `resolver` resolves module specs to source (ADR-004); M1 uses an
    injected map, M2/M4 the any-backed one."""
    env_map = env or {}
    resolve = resolver or DictResolver({})
    _probe_cache: dict = {}  # objectId -> (marker, Resolved)  ADR-004 §4

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

    @effect("module.resolve", kind="read", registry=registry)
    def module_resolve(ctx, spec, frm=None):
        # ADR-004 §3/§4: host resolves + probe-validated cache. The
        # record is self-contained (carries source) so strict replay
        # rebuilds modules bit-exact even on a cache hit.
        r = resolve(spec, frm)
        cached = _probe_cache.get(r.object_id)
        if cached is not None and cached[0] == r.marker:
            cache_state = "hit"
            r = cached[1]
        else:
            cache_state = "miss"
            _probe_cache[r.object_id] = (r.marker, r)
        return {
            "spaceId": r.space_id,
            "objectId": r.object_id,
            "marker": r.marker,
            "sourceHash": r.source_hash,
            "source": r.source,
            "cache": cache_state,
        }

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
    def trace_effects_of(ctx, cell=None, span=None):
        # Compact per-cell/per-span view (ADR-003 §4; span filter feeds
        # the toolcaller's inner-cell digests). No input/output bodies.
        out = []
        for r in ctx.writer.records:
            if r["kind"] == "effect" \
                    and (cell is None or r.get("cell") == cell) \
                    and (span is None or r.get("span") == span):
                entry = {
                    "seq": r["seq"],
                    "effect": r["effect"],
                    "class": r.get("meta", {}).get("class"),
                    "mocked": r.get("meta", {}).get("mocked"),
                    "error": (r.get("error") or {}).get("type"),
                }
                if "span" in r:  # grouping stamp (ADR-001 §4c)
                    entry["span"] = r["span"]
                out.append(entry)
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
