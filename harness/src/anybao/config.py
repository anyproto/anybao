"""Config effect — ADR-006 §3 / plan §4 config effect.

Config-as-data: a derived per-space object holding records keyed by
dotted key (`llm.tier.codegen`, `loop.max_turns`). Cascade resolution
`localValue ?? value ?? default` (device→space→default; account tier
deferred to SDK). Secrets are localValue-only, enforced. Resolution is
pure over a ConfigStore (AnyConfigStore in prod, dict in tests); key
bytes are only ever read INSIDE effect implementations (ADR-002 ctx).
"""

from __future__ import annotations

from typing import Any, Protocol


class ConfigStore(Protocol):
    def records(self) -> dict[str, dict]: ...          # key -> {value?, localValue?}
    def put(self, key: str, field: str, value: Any) -> None: ...  # field: value|localValue


class DictConfigStore:
    """Test/bootstrap store: in-memory records."""

    def __init__(self, records: dict[str, dict] | None = None):
        self._r = records or {}

    def records(self) -> dict[str, dict]:
        return self._r

    def put(self, key: str, field: str, value: Any) -> None:
        self._r.setdefault(key, {})[field] = value


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, store: ConfigStore):
        self._store = store
        self._defaults: dict[str, Any] = {}
        self._secret: set[str] = set()

    def define(self, key: str, *, default: Any = None, secret: bool = False) -> None:
        self._defaults[key] = default
        if secret:
            self._secret.add(key)

    def get(self, key: str) -> Any:
        rec = self._store.records().get(key, {})
        if "localValue" in rec:          # device scope wins
            return rec["localValue"]
        if "value" in rec:               # space (synced) scope
            return rec["value"]
        if key in self._defaults:
            return self._defaults[key]
        raise ConfigError(f"no config value for {key!r}")

    def set(self, key: str, value: Any, *, scope: str = "space") -> None:
        # ADR-006 §3: secrets refuse synced scope (CRDT history is
        # forever) — enforced, not documented.
        if key in self._secret and scope != "device":
            raise ConfigError(
                f"{key!r} is a secret — writable at device scope only "
                f"(synced values live in CRDT history forever)"
            )
        field = "localValue" if scope == "device" else "value"
        self._store.put(key, field, value)


def register_config_effect(registry, config: Config) -> None:
    """Expose non-secret config to cells via a `config.get` effect. Secret
    keys are never served to guest code — only effect impls read them
    (they call config.get on the Python object, not through this effect)."""
    from anyrt.effects import effect

    @effect("config.get", kind="read", registry=registry)
    def config_get(ctx, key):
        # Cells/programs get non-secret config only. INFRASTRUCTURE
        # secrets (LLM key) are resolved host-side by their effects.
        # INTEGRATION secrets a program needs (e.g. gemini) are USED via
        # named-credential injection, never READ — plan §4 config
        # (named-credential injection, M6), not this door.
        if key in config._secret:
            raise ConfigError(f"{key!r} is a secret — not readable from cells")
        return {"value": config.get(key)}
