# Config secrets — seeding, rotation, storage

Config secrets (the Anthropic key, connector keys, any other ref)
persist **account-scoped** — the synced `value` of their row (ADR-021
§4; any-sync end-to-end encrypts every change, the bao space is
owner-only) on the per-space **secrets object** — the derived `agent_secrets` dataset
(seed `any/agent-secrets/v1`, reported as
`SpaceInfo.agentSecretsObjectId`), split out of `agent_config` so
config stays guest-readable while secrets are not. Env vars are **not
read** (removed 2026-07-28); the only seeding paths are the ones below.

## Seeding and rotation: `.connectors.env` / `--secrets-file` (hard seeds)

Put a dotenv-style `.connectors.env` **next to the config file**
(fallback: cwd) — or pass any such file explicitly with
`anyrt serve --secrets-file <path>` — keyed by the SECRET REF itself:

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
lines are skipped. These files hold plaintext keys — `.connectors.env`
is gitignored; keep it that way.

Managed OAuth refs (`connector.oauth.<provider>.refresh` /
`.client_id` / `.client_secret`, ADR-011 §3) seed and revoke through
the same file. One difference matters: an empty
`connector.oauth.<provider>.refresh=` deletes the stored token
**locally only** — the grant stays live on the provider account; run
the provider's `disconnect()` to revoke provider-side (ADR-011 §8).

Both paths feed the same map (`Config::secret_overrides`); when a ref
appears in both, `--secrets-file` wins. They are boot-time seeds only.

## Entering keys while running (ADR-021)

The stored row **is** the credential: the broker reads the
`agent_secrets` row at injection time (after the effect is recorded),
so a key written while serve runs — from ANY of the account's devices,
a phone included — is used by the very next credentialed effect. No
restart. Writers:

- **The chat prompt.** When a credentialed effect finds no value the
  host stamps the row `status: "missing"` (with the connector's
  descriptor — label, hosts, help) and posts one request bubble into
  the chat (`attachments.credreq.type = "credential_request"`); the
  UI renders it as an inline form and, after writing the row, sends a
  user message with a `credential_set` attachment that the agent
  retries on. A missing LLM key takes this path too — the request is
  posted by the host, no model turn needed.
- **Credentials** in the app: every row, upsert/delete.
- **Help → Import connector keys** in any-ui: the same per-row write
  for each entry of a picked .env (`AgentHandle::set_secret`, the lib
  embedder's write; empty value deletes).

Row metadata is synced and non-secret: `status` (`missing`|`set`),
`updatedAt`, `label`, `hosts` (the destinations the key is meant for —
shown at entry; enforcement is ADR-021 §7), `help`, `note`,
`requestedBy` (the run that missed it), `requestedIn`/`requestedAt`
(the chat holding a live request bubble; cleared by the write that
sets the value). The value is the row's synced `value` field —
readable by the account's own devices only, stripped by the UI at its
read seam.
`config.get` refuses the `connector.key.*`, `llm.key.*` and
`connector.oauth.*` namespaces wholesale.

`anyrt run` accepts the same `--secrets-file` (and reads
`.connectors.env`), but a one-shot run has no store: seeds are this
run's in-memory map only — nothing is persisted, and empty values are
simply dropped. The map is the **no-store fallback** (also serve
against a server without the secrets object); with a store it is
never consulted.

## Soft seeds (embedder bootstrap)

Entries an embedder places in `Config::secrets` (e.g. a bundled
demo-keys resource) are **soft**: used this run, persisted only when
nothing is stored, never overwriting. Precedence: **hard seeds >
stored > soft seeds**.

## Later starts

Boot writes the seeds through and logs what the store holds — every
secret-marked record, any ref:

```
config: llm.key.anthropic loaded from device-local store
```

then drops the in-memory map: with a store, the broker reads rows.
No anthropic key anywhere → serve still starts but warns; the first
conversation gets a credential prompt instead of a reply. A fresh
space (or a deleted-and-recreated one) has an empty store.

## Guest read-guard

The runtime refuses guest http requests that reference the
`agent_secrets` dataset (exact `dataset` field match — any space) or
the guarded object id — a `forbidden` EffectFailure carrying the
import instructions, raised **before** execution so the refusal is the
recorded fact and no secret ever reaches a trace or the model context.
Guest code never needs the values: the host injects `credential:
{ref}` headers after recording (ADR-008), and a missing key surfaces
as a typed `SecretMissing` failure (message `no secret for credential
ref "<ref>"`, which connectors map to their not-connected error) plus
the chat prompt above.

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
