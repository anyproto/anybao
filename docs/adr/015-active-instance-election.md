# ADR-015: Active-instance election — devices registry consumer

Status: **Accepted** (2026-08-17), amended 2026-08-18 (§4: the SDK
review pass hardened tombstone semantics — 409 `device.pruned` on
self-row writes is now PERMANENT STANDBY, never the disabled-active
degrade; plus a brief boot retry now that registry unavailability is
an error rather than an empty read)
Date: 2026-08-17
Builds on: ADR-006 §4 (triggers: single-owner, missed-occurrence
rule), ADR-009 §6 (serve/lib surface), ADR-009 §8 (snapshot backlog —
the catch-up mechanism takeover reuses)
Companion: SYN-165 (`any` + SDK: tech-space `devices` dataset,
`/v1/devices` API, server-computed winner) — the runtime side is this
ADR; the registry model, election contract, and decision matrix are
documented in the `any` repo (`docs/21-devices.md`).

## Context

Two `anyrt serve` processes on the same account (laptop + mac) both
watch the same general chat and both answer — the live incident: a
sleeping mac's serve woke, replayed its backlog, and double-answered
alongside the current device ("prod serve split-brain", 2026-08-11).
Nothing in the harness knows which device *should* be the agent.

SYN-165 adds the missing substrate: a `devices` dataset in the tech
space (row per device: peer id, name, os, version, `apps.<slug>`
presence, `activeClaims.<slug>` = `{seq, at}`), exposed through a
system `/v1/devices` endpoint. The critical design point decided
there: **the winner rule lives server-side** — `GET /v1/devices`
returns a computed `active: {<slug>: <peerId>}` map (highest claim
`seq`, tiebreak `at`, then peerId), so the runtime and any UI consume
the same verdict and neither reimplements the tiebreak. This ADR
covers the only piece that lives in anybao: the **election consumer**
that registers the device, claims when appropriate, and gates the
serve loops on the verdict.

## Decision

### 1. App slug and registration

The registry is app-generic; anybao is the app slug **`bao`**
(constant, not config — one account runs one bao; the agent *name* is
presentation, not identity). On every serve boot, after the client
handshake:

- `PUT /v1/devices/me` with `{"apps": {"bao": {"version":
  <crate version>}}}` — presence + version only. The server stamps
  peerId/os/hostname on the self row itself (engine-boot upsert,
  SYN-165 phase 2); the runtime never writes another device's row.
- The self peer id comes from the reply (`GET /v1/devices` `self`
  field, falling back to the PUT reply's row id) — the runtime has no
  other route to its own peer id.

### 2. The decision rule (consumer side)

Per reconcile (one `GET /v1/devices`), with `winner =
active["bao"]`:

1. `winner == self` → **active**.
2. no *other* device row carries `apps.bao` → **claim** (the majority
   case: first/only bao — even when a dangling claim points at a
   pruned-away device; a deleted row is the "device doesn't exist"
   signal, per SYN-165).
3. `winner` is another device whose row exists with `apps.bao` →
   **standby**.
4. otherwise (other bao rows exist but no live winner — the active
   device's row was deleted) → **claim**; concurrent survivors both
   claim and the server tiebreak converges them, losers stand down on
   the next reconcile.

A **claim** is `POST /v1/devices/activate {"app": "bao"}` followed by
an immediate re-fetch to verify (a concurrent claim may out-seq
ours); the runtime never un-claims another device — only claims and
row deletions move the winner (decision matrix, SYN-165).

### 3. The gate

Serve carries one `active: AtomicBool` (on `RunCtx`), written ONLY by
the election thread. Standby means:

- **Chat watch disconnected** — not merely muted. Nothing lands in
  the watcher's seen-set, so takeover's reconnect snapshot yields the
  full unanswered backlog (ADR-009 §8). Both devices post under the
  same `agent.name`, so the other device's replies terminate the
  backlog scan correctly. No "not ready" bubbles from a standby.
- **Trigger ticker idle** — no runs, no dataset adoption, no
  ownership stamping. At boot in standby, the standing-trigger
  records are not upserted either (registry stays in-memory).
- `ctx.run()` itself is NOT gated — embedder/CLI runs are explicit.

Transitions (election thread only):

- **Takeover** (false→true): re-arm every cron strictly forward
  (`next_due = None` — a missed occurrence while standby does not
  exist, the ADR-006 §4 cold-sync rule; prevents the wake-and-replay
  burst that caused the incident), stamp + upsert the registry's
  trigger records, then flip the flag. Chat watch reconnects and
  drains the snapshot backlog.
- **Stand-down** (true→false): flip the flag, clear the deferred
  backlog (the new active device answers those), let in-flight runs
  finish (same as shutdown — never interrupt a turn mid-flight).

### 4. Cadence and degrade

The election thread polls `GET /v1/devices` every 10s (localhost
read; manual-switch latency ≤ one poll). A subscribe upgrade
(`/v1/devices/query/subscribe`) can replace the poll later without
contract change — the reconcile is idempotent. Errors keep the last
verdict (a transient read failure must not flap the gate).

Degrade, three distinct verdicts at boot:

- **404** on `PUT /v1/devices/me` (server predates SYN-165): election
  disabled for the run — gate permanently true, no thread, one log
  line. Today's single-device behavior, unchanged.
- **409 `device.pruned`** (this device's row was tombstoned — sticky,
  only a fresh `any init` re-registers): **permanent standby**, gate
  false, no thread, loud warn. An excommunicated device answering as
  the active bao is exactly the split-brain the registry prevents, so
  pruned must never fall into the availability degrade. A claim
  hitting `device.pruned` mid-run stands down the same way.
- **Other errors**: brief bounded retry (serve's boot order guarantees
  the techspace is open — `list_spaces` already ran — so this is a
  freak), then disabled-active for the run: availability over
  strictness, logged.

### 5. Observability

Control API `GET /election` →
`{app, enabled, active, peerId, winner}` (winner via a live
registry read, null when unavailable).

## Consequences

- Split-brain becomes a ≤ poll-interval race window instead of a
  standing state; the survivable-collision stance (concurrent claims,
  server tiebreak) is inherited from SYN-165, not re-decided here.
- A standby serve is cheap: no SSE stream, no trigger runs, one
  registry read per 10s.
- The runtime trusts the server's `active` map blindly — by design
  (one implementation of the rule). If the map is wrong, the fix is
  server-side.
- Election state is per-process and rebuilt at boot; nothing new
  persists in anybao's own datasets.
