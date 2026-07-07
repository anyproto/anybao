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

    @effect("kernel.boot", kind="read", registry=registry)
    def kernel_boot(ctx, **pins):
        # Input carries the determinism pins (kernel hash, hashseed,
        # schema); recording it makes them part of the run — a changed
        # kernel diverges in strict replay, which is correct.
        return None
