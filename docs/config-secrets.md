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

## Rotation and revoke: `.connectors.env` (hard seeds)

Env seeding can only bootstrap — it never overwrites a stored value. To
rotate (or revoke) keys, put a dotenv-style `.connectors.env` **next to
the config file** (fallback: cwd), keyed by the SECRET REF itself:

```
connector.key.linear=lin_api_…
connector.key.github=github_pat_…
llm.key.anthropic=sk-ant-…
connector.key.granola=          # empty value DELETES the stored secret
```

On every serve start, each ref in the file is written through to the
device-local store: missing → `bootstrapped`, different → `rotated
(hard seed)`, empty → `removed`. Refs absent from the file are
untouched, so the file need not be complete. The ref set is **open** —
any `connector.key.<x>` (or other ref) is stored, so a new connector
needs no runtime change. Comments (`#`) and quotes are fine; malformed
lines are skipped. This file holds plaintext keys — it is gitignored;
keep it that way. Lib embedders (any-ui) feed the same mechanism from
memory via `Config::secret_overrides` /
`ConfigBuilder::secret_override` instead of a file.

## Rules

- **Precedence: hard seeds > stored > env.** A `.connectors.env` entry
  (or embedder override) always wins and writes through; otherwise the
  stored device-local value is authoritative — an env var present on a
  later start is a noop (it does not overwrite the store).
- **Both missing → warn, not persist.** If neither the store nor the env
  has a key, serve still starts but logs
  `WARN config: no anthropic key …`; llm effects fail on first use until
  you provide one (re-run once with the env var set).
- **Fresh space re-bootstraps.** A new space (or a deleted-and-recreated
  one) has an empty store, so its first start needs the env var again.
- **Deploy before the first serve.** serve is space-only (ADR-009 §5):
  it reads programs, skills, and the kernel from the space — run
  `anyrt deploy` once against a fresh space (or its `agent` overlay)
  before starting it.
- **Server requirement.** The `any` server must declare `localValue`
  local-scope on the `agent_config` dataset (`internal/agentconfig`). An
  older server rejects the local write; serve then logs
  `could not persist … using env value this run` and you stay on the
  env-every-start path until the server is updated.

Every ref in `config::PROVIDER_SECRET_REFS` env-bootstraps and
persists this way (anthropic, gemini, together, and the connector keys
— see the connectors README auth table for the env-var names); the
Anthropic key is the only one whose absence warns. Stored secrets load
generically — any secret-marked config record, not just the provider
list.
