# Config secrets — seeding, rotation, storage

Config secrets (the Anthropic key, connector keys, any other ref)
persist **device-locally** as never-synced local values (ADR-006 §3) on
the per-space **secrets object** — the derived `agent_secrets` dataset
(seed `any/agent-secrets/v1`, reported as
`SpaceInfo.agentSecretsObjectId`), split out of `agent_config` so
config stays guest-readable while secrets are not. Env vars are **not
read** (removed 2026-07-28); the only seeding paths are the ones below.

## Seeding and rotation: `.connectors.env` (hard seeds)

Put a dotenv-style `.connectors.env` **next to the config file**
(fallback: cwd), keyed by the SECRET REF itself:

```
llm.key.anthropic=sk-ant-…
connector.key.linear=lin_api_…
connector.key.github=github_pat_…
connector.key.granola=          # empty value DELETES the stored secret
```

On every serve start, each ref in the file is written through to the
device-local store: missing → `bootstrapped`, different → `rotated
(hard seed)`, empty → `removed`. Refs absent from the file are
untouched, so the file need not be complete. The ref set is **open** —
any `connector.key.<x>` (or other ref) is stored, so a new connector
needs no runtime change. Comments (`#`) and quotes are fine; malformed
lines are skipped. This file holds plaintext keys — it is gitignored;
keep it that way.

In any-ui the same mechanism is fed **from memory** (no file
persisted): Help → Import connector keys parses the picked .env into
`Config::secret_overrides` and restarts the agent. Lib embedders use
`ConfigBuilder::secret_override`.

## Soft seeds (embedder bootstrap)

Entries an embedder places in `Config::secrets` (e.g. a bundled
demo-keys resource) are **soft**: used this run, persisted only when
nothing is stored, never overwriting. Precedence: **hard seeds >
stored > soft seeds**.

## Later starts

Stored secrets load generically — every secret-marked record, any ref:

```
config: llm.key.anthropic loaded from device-local store
```

No anthropic key anywhere → serve still starts but warns; llm effects
fail on first use until one is imported. A fresh space (or a
deleted-and-recreated one) has an empty store — re-import.

## Guest read-guard

The runtime refuses guest http requests that reference the
`agent_secrets` dataset (exact `dataset` field match — any space) or
the guarded object id — a `forbidden` EffectFailure carrying the
import instructions, raised **before** execution so the refusal is the
recorded fact and no secret ever reaches a trace or the model context.
Guest code never needs the values: the host injects `credential:
{ref}` headers after recording (ADR-008), and a missing key surfaces
as each connector's actionable not-connected error.

## Upgrading from the pre-split layout

There is **no migration**: secrets left on an old config object are
ignored — re-import your keys once (Help → Import connector keys, or
a `.connectors.env` and a restart). Serve warns loudly when it finds
no secrets, and until the anthropic key is imported the llm effects
fail, so the situation is obvious rather than silent.

## Server requirements

The `any` server must provide `internal/agentsecrets` — the derived
secrets object + `agent_secrets` dataset with the local-scope `value`
field, reported as `SpaceInfo.agentSecretsObjectId` (anyproto/any#142).
An older server means no stored secrets at all: serve warns and runs
on whatever the seeds supplied this run. Deploy before the first serve
as usual (ADR-009 §5): serve is space-only.
