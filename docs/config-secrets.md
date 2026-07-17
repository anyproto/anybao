# Config secrets — first-time bootstrap

Config secrets (today: the Anthropic API key) persist **device-locally**
on the per-space config object — a never-synced `localValue` record
(ADR-006 §3). You seed them from the environment **once**; after that the
env var is no longer needed.

## First start (seed from env)

Before the **first** `anyrt serve` for a space, have the key in your
environment — e.g. source your `.env`:

```fish
source .env            # exports ANTHROPIC_API_KEY (and any other keys)
make runtime
./runtime/target/release/anyrt serve --addr http://127.0.0.1:7001 --space bao
```

On this first start serve persists the key device-locally and logs:

```
config: anthropic key bootstrapped to device-local store
```

## Later starts (no env needed)

The key now lives on the config object (never synced off this device), so
later starts need **no** env var:

```fish
./runtime/target/release/anyrt serve --addr http://127.0.0.1:7001 --space bao
# config: anthropic key loaded from device-local store
```

## Rules

- **Stored value wins.** Once persisted, the device-local value is
  authoritative — an env var present on a later start is a noop (it does
  not overwrite the store).
- **Both missing → warn, not persist.** If neither the store nor the env
  has a key, serve still starts but logs
  `WARN config: no anthropic key …`; llm effects fail on first use until
  you provide one (re-run once with the env var set).
- **Fresh space re-bootstraps.** A new space (or a deleted-and-recreated
  one) has an empty store, so its first start needs the env var again.
- **Server requirement.** The `any` server must declare `localValue`
  local-scope on the `agent_config` dataset (`internal/agentconfig`). An
  older server rejects the local write; serve then logs
  `could not persist … using env value this run` and you stay on the
  env-every-start path until the server is updated.

Only the Anthropic key auto-persists today; other keys
(`GEMINI_API_KEY`, `TOGETHER_API_KEY`) still come from the environment.
