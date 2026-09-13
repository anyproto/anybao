# ADR-025: Bao presence & status line over the event bus

Status: **Proposed** (2026-08-31), amended 2026-09-13 (§1/§2/§6: the
beat carries the election role and the claim holder — a standby
device beats like an active one and must never read as "bao is
online", BOB-111)
Date: 2026-08-31
Builds on: ADR-005 (progress bubbles — related family, NOT migrated
here), ADR-014 (progress: a module owning its transport), ADR-015
(election identity), ADR-023 (run summaries — the fallback text
source), any doc 21 (event bus envelope), api-parity.md's 2026-07-08
pub/sub note
Tasks: BOB-73; BOB-111 (`role`/`winner` in the beat), BOB-117 (the
UI consumer of §6)

## Context

The UI fakes bao's online/activity status client-side (local timing
heuristics over chat messages); the truth — a serve process with a
live run — never reaches the UI (BOB-73). The event bus (any doc 21)
has since landed: an account-scoped ephemeral envelope with an open
type set, no server change needed for new kinds. The api-parity note
(2026-07-08) earmarked exactly this family for pub/sub.

Review raised the authoring question for the human-readable line: a
cheap model summarizing the run, or bao itself sending status via a
tool. Decision below: the tool, with decay and a deterministic
fallback — an LLM never sits in the presence path.

## Decision

### 1. Two layers, one event type

**Layer 1 — machine presence.** Deterministic, serve-published,
never model-generated. "Is bao alive" is a heartbeat question; it
must not depend on a model having a good day.

**Layer 2 — status line.** Bao-authored prose ("migrating the mail
dataset, ~60% through the backlog") via a tool (§3), TTL-decaying,
with a serve-side fallback.

Both ride one envelope:

```jsonc
{
  "type":    "bao.status",
  "scope":   "account",
  "target":  "<serving peer id>",       // ADR-015 identity, filterable
  "data": {
    "identity":  "<peer id>",
    "state":     "boot" | "idle" | "working" | "shutdown",
    "role":      "active" | "standby",   // the election verdict (ADR-015 §3)
    "winner":    "<peer id>",            // the registry's claim holder; absent when none
    "run": {                             // iff working
      "id": "…", "title": "…", "startedAt": 123.0,
      "cells": 12,                       // tool calls so far (cell+bash spans)
      "cell": "c.query(space, …"         // newest cell's preview; absent before the first
    },
    "line":      "…",                    // bao-authored; absent when unset/stale
    "lineAt":    123.0                   // when bao last set it
  }
}
```

`state` describes the run loop; `role` says whether this device is
the one that answers (the ADR-015 §3 gate). A standby serve beats
too — `idle`, same cadence — so a consumer that reads liveness alone
cannot tell it from the working agent; only `role` can. `winner` is
the claim holder of the reconcile that produced the verdict (the
same peer `GET /election` reports), so naming the device that
answers costs no registry read on the beat path. Election disabled
(server predates the registry) beats `active` with no `winner`; a
pruned device beats `standby`.

Timestamps are unix seconds — staleness math is the consumer's job.
At-most-once bus ⇒ the payload is an idempotent full-state write
(last-write-wins), same rule as task-status. `run` is joinable to
`process.*` progress bars (ADR-014) by id — status references, it
does not duplicate them.

`working`/`run` derive from `RunCtx::live_runs` — the registry the
hard-break control path (ADR-005 §3) already keeps, entries living
exactly as long as `run_program`. Each entry carries an `{id, title,
startedAt}` stamp (title = the user's message preview on chat runs,
else the program spec); the freshest stamp is the beat's `run`. One
source of truth for "what's running" serves `/break`, presence and
the control API's `GET /status` alike — never a parallel counter.

`cells`/`cell` come from the same entry's `RunActivity` (an Arc the
run's Broker shares with its `LiveRun`): every tool call already
crosses the host as a `cell` span begin (`bash` spans carry their
command; the toolcaller adds a collapsed ≤48-char `preview` to cell
span inputs), so the broker counts spans and keeps the newest
preview. Deterministic host observation of what the model is doing —
no model in the loop, same rule as the rest of layer 1.

### 2. Cadence & staleness

Serve beats every 10s from a dedicated presence thread, and
republishes within its 1s poll on ANY change in what the beat would
say — a line set, a run starting or ending, a new tool call, a
takeover or stand-down (the change signature). Working/idle flips,
the call counter and the role are
therefore ~1s behind reality, never a full beat; the cadence beat is
the liveness floor. Budget: one event per tool call ≈ one per few
seconds on a busy run — far under the bus's 30 msg/s cap. The UI
marks offline after 3 missed beats (30s). A graceful shutdown publishes
`state: "shutdown"` once for instant offline; a crash is covered by
the TTL. No fresh beats at all ⇒ offline — the pre-BOB-73 default.

### 3. The status tool — `status@v1`

A small agent-tool module owning its transport, the `progress@v1`
pattern (ADR-014): `set(line)` only. The guest does NOT publish
events directly — the line crosses the effect boundary (ADR-002)
into serve state; serve folds it into every beat and republishes
immediately on set. One publisher, one envelope, TTL in one place.

- **Decay:** the line drops out of the payload 90s after `lineAt` —
  a forgotten update degrades to machine truth instead of lying.
- **Fallback:** when the line is absent/stale while `working`, the
  UI's display slot falls back to the beat's `run.title` — the live
  stamp (§1), deterministic and always present on working beats.
  Bao's line is an upgrade, not a single point of failure. (The
  ADR-023 summary title exists only at dump, too late for a live
  beat.)
- **Nudge, not enforcement:** one line in the agent guidance ("on
  long runs, update your status line"); the decay makes silence
  safe.

### 4. Rejected: a cheap-model summarizer

- Its best input — bao's narration, the run record — already
  exists verbatim; it would be a lossy paid copy.
- Status wants freshness; a model call adds latency and per-update
  cost to a line glanced at for two seconds.
- A confident "fixing the database" while bao does nothing of the
  sort is worse than a terse title. Status lies are worse than
  terse status.

Revisit only with evidence bao writes bad lines — the expectation
is it never earns the revisit.

### 5. UI

A `baoStatus` atom fed by the ONE existing bus subscription
(`ui.*`/`process.*` stream) with `bao.status` as a third filter,
plus a staleness clock. Two consumers:

- **Status bar** (the BOB-73 deliverable, and the ONE place activity
  detail renders): a shell-mounted source LEADING the whole cluster
  (leftmost, before the view facts — owner call) — a BaoFace as the
  item's icon (happy when idle; typing mood with a slow breathe-pulse
  while working — the identity + the established "alive" animation,
  chosen over an anonymous pulsing dot). Label while working: the
  line, else the newest cell's preview, else `run.title`; the
  tool-call count rides the tooltip so the bar stays quiet. Nothing
  rendered when offline. Cold start: no replay on the bus, so a fresh
  window is "unknown" for up to one beat (≤10s) — render nothing
  until the first beat.
- **Chat**: NO typing indicator at all (owner call after rig
  testing) — the row is deleted; a run's liveness in chat is the
  `done:false` bubble stream itself plus the bar. The working label
  in the bar carries the `. → .. → ...` ticker (500ms) as the alive
  cue. The `deriveAgentTyping` pending-send derivation remains (the
  credential cards arm it; it stays the canonical resolution) with
  no rendering consumer.

ADR-005 `done:false` progress bubbles are NOT migrated here —
presence/status only; the narration migration stays a separate
revisit (api-parity note).

### 6. Trust & multi-device

`sender` is server-stamped (self: true, own account only) — no
client-supplied identity on the wire. Several serves beat at once as
a matter of course: every standby device (ADR-015 §3), plus
pre-election overlap and the remote-runner future. The UI dedups by
`identity` and keys "online" to the ROLE, never to liveness alone:

- bao is online iff a fresh beat carries `role: "active"`;
  working-state comes from that beat.
- fresh beats that are all `standby` mean "another device holds
  bao" — `winner` names it (the devices registry has its name,
  ADR-015 §5) — and the active device is offline if it does not beat
  itself. Never "online".
- no fresh beat at all ⇒ offline.

The switch is the user's, explicit, on the device they want (ADR-015
§2/§5): the UI names the offline claim holder and offers "use this
device" — BOB-117 owns that surface.

### 7. Testing

Serve: `StubTransport` assertions on the publish sequence (boot →
idle → working + run title → idle → shutdown; line set + decay;
set republishes immediately; a role flip republishes within a poll,
the envelope carries `role` + `winner`). UI: atom + staleness tests.
