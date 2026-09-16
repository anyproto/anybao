# ADR-015: Active-instance election — devices registry consumer

Status: **Accepted** (2026-08-17), amended 2026-08-18 (§4: the SDK
review pass hardened tombstone semantics — 409 `device.pruned` on
self-row writes is now PERMANENT STANDBY, never the disabled-active
degrade; plus a brief boot retry now that registry unavailability is
an error rather than an empty read), amended 2026-08-24 (§3: the gate
narrowed for device-pinned triggers, ADR-006 §4 — standby no longer
idles the whole ticker), amended 2026-09-13 (§3/§5: one verdict
snapshot; the claim holder rides the presence beat beside the
responder role, and not answering chat repeats itself in the log,
BOB-111), amended 2026-09-16 (§5: the log speaks only on a change of
chat ownership — the once-a-minute repeat is gone; the role is
visible in the UI and on `GET /status`, BOB-143 follow-up; §5: the
guest's `list_devices` rows carry `self`/`active`/`bao` flags and the
`_any` skill says where the user switches, BOB-117)
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

Serve carries one verdict snapshot (`RunCtx::verdict`: the gate +
the claim holder of the reconcile that produced it), written ONLY by
the election thread, as a whole, after a takeover has re-armed —
never a gate from one reconcile beside a winner from another.
Standby means:

- **Chat watch disconnected** — not merely muted. Nothing lands in
  the watcher's seen-set, so takeover's reconnect snapshot yields the
  full unanswered backlog (ADR-009 §8). Both devices post under the
  same `agent.name`, so the other device's replies terminate the
  backlog scan correctly. No "not ready" bubbles from a standby.
  (Amended 2026-08-24, ADR-018 §3: the watch's gate is no longer
  `active` itself but OWNERSHIP of the enabled `chat-watch` record —
  the election moves that record on takeover, so the default is
  unchanged, while a repin can hand chat to a standby device.)
- **Trigger ticker: election-scoped, not idle** (amended 2026-08-24,
  with ADR-006 §4 device pinning): the ticker runs on standby too and
  fires the records PINNED to this device — pins are
  election-independent. What standby withholds: adopting unowned
  records, the deferred-conversation drain (which follows `chat-watch`
  ownership, ADR-018 §3), and the standing built-ins (seeded ownerless at a standby boot — not runnable, not
  upserted — until takeover stamps them). A PRUNED device (§4) fires
  nothing at all, pins included.
- `ctx.run()` itself is NOT gated — embedder/CLI runs are explicit.

Transitions (election thread only):

- **Takeover** (false→true): for the election-following set only —
  the STANDING built-ins and the `chat-watch` record (ADR-018 §3);
  pinned user records were never idle and never move on an election
  flip — re-arm the crons strictly forward (`next_due = None` — a
  missed occurrence while standby does not exist, the ADR-006 §4
  cold-sync rule; prevents the wake-and-replay burst that caused the
  incident), stamp + upsert their records (`chat-watch` is read back
  from the dataset — a standby never held it), then flip the flag.
  Owning `chat-watch` reconnects the watch, which drains the snapshot
  backlog.
- **Stand-down** (true→false): flip the flag, clear the deferred
  backlog (the new active device answers those), clear the standing
  built-ins' local ownership so they stop firing here (no record
  write — the new winner's takeover stamps them), and RELEASE
  `chat-watch` on the record (owner cleared — the one stand-down
  write: a local-only evict would be undone by the next reconcile,
  which still reads this peer id), let in-flight runs finish (same as
  shutdown — never interrupt a turn mid-flight). Device-pinned records
  keep firing through the transition.

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
`{app, enabled, active, peerId, winner}` — the verdict snapshot (§3),
never a live registry read, so it always agrees with the beat.

The presence beat carries `winner`, the snapshot's claim holder, and
`role`, which is NOT the gate but chat-responder ownership (ADR-025
§1; the gate does not address chat, ADR-018 §3) — so any bus
consumer can tell the device that answers from one that does not
without a registry read; `GET /status` shows the same envelope. A
role or winner change republishes within the presence poll.

Not answering chat is a silent state: no chat watch, no runs. The
log records every transition of it and nothing while it holds: the
boot line names the winner (`election: peer … — standby (the active
bao is peer …)`), and the presence thread logs one line each time
the REASON this device does not own the enabled responder changes —
`election: standby (the active bao is peer …) — chat is not answered
here` (a new line when the winner moves), the pruned variant, or
`chat: this device is the active bao but does not own the enabled
chat responder (paused, or pinned to another device) …` — and one
line when it answers again (`chat: answered here — this device owns
the enabled chat responder`). The first observation after boot is
the baseline, not logged: the boot line already said it. A state
that holds is never repeated — the role is live in the UI (BOB-134)
and on `GET /status`, so the log's job is the history of flips, not
a heartbeat. The line rides the presence poll, not the election
thread, so a flip still lands while registry reads fail (those get
one `election: registry read failed` warning and one recovery line)
and on a pruned device, which runs no election thread.

Guest: `any@v1.list_devices()` (getter, public) is the registry read
— `{self, active, devices}` — with each device row flagged `self`
(the device this run executes on), `active` (holds the bao claim)
and `bao` (has run bao), so bao can tell the user which device
answers right now, which others exist and where to switch (Settings
▸ Agent ▸ Devices ▸ "Use this device", on the device they want) —
the `_any` skill carries that guidance, including the ≤ one-poll
hand-off window a quick switch-back can expose (BOB-143).
Read-only by design: the rule stays server-side (§2) and a switch is
the user activating on the device they want (§4 cadence); no guest
`activate` exists, the election claims only for its own process (§3).

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
