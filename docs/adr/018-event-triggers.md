# ADR-018: Event triggers — chat-message sources, and the chat watcher as a trigger

Status: **Accepted** (2026-08-24)
Date: 2026-08-24
Builds on: ADR-006 §4 (trigger contract, device pins, reconcile),
ADR-009 §8 (chat watch, snapshot backlog, deferred conversations),
ADR-015 (devices election)
Amends when accepted: ADR-006 §4 (owner-state vocabulary, event
spec), ADR-009 §8 (the watcher's home), ADR-015 §3 (the
election-following set)

## Context

`kind: "event"` has been declared in the trigger shape since ADR-006
§4 and never evaluated — since 2026-08-24 the health pass stamps such
records `unsupported_kind` instead of leaving them silently inert.
Meanwhile the device-pin work made triggers device-addressable: a
record's `owner` says where it runs, the UI repins by writing it. The
one background behavior that is NOT a trigger is the biggest one: the
chat watcher — hardcoded in serve, gated by the election, invisible in
Scheduled, not repinnable. "Which device answers as bao" deserves to
be the same visible, movable thing "which device runs this cron" now
is. Event triggers are the vehicle: implement the kind with a
chat-message source, then make the watcher a standing event-trigger
record.

A second motivation is the remote-runner future: agents/VMs that hold
pins and fire work where they live. Event sources must therefore ride
the same ownership/reconcile machinery as cron — no side-channel
subscription registry.

## Decision

### 1. Every trigger is pinned; "unassigned" is transient (ADR-006 §4 clarified)

The record is the single truth about where a trigger runs — a device
decides "do I run this?" by comparing `owner` with its own peer id,
never by consulting the election at run time. That is the local-first
property worth protecting: no two devices have to agree on a computed
verdict per tick, and the ≤ one-poll election-overlap window (ADR-015)
touches nothing that already has an owner.

Owner states:

- **peer id — pinned**: runs on that device only, online or not.
  Written by the claim below, by a UI repin, or by an agent.
- **`""` — unassigned, transient**: the election-active device CLAIMS
  it on its next tick and stamps its peer id (the reconcile as
  shipped 2026-08-24). Two claimers in the overlap window converge
  through the record (one owner value wins; the other evicts) —
  the only place the election touches a user trigger.
- **legacy `anyrt-<pid>`**: read as unassigned (unchanged).

Consequences that follow, and are intended:

- Moving the active bao does NOT move user triggers — a pin is a pin,
  whether the device is offline or merely no longer active. Moving
  one is an explicit act: repin it, or clear its owner ("reassign to
  the active device") and let the current winner claim it.
- The election-following set is exactly the "this is bao" set: the
  standing built-ins (as today — takeover re-stamps them, an explicit
  write) and, per §3, `chat-watch`. Nothing else moves implicitly.
- The UI label for an empty owner must say what it is — "Unassigned
  (the active device will claim it)" — not "Any active device", which
  described a follow-the-election mode this ADR rejects. The device
  row shows the real owner once claimed, which is the common state.

### 2. The event kind

- **Spec** (the ADR-006 shape, narrowed): `{dataset, objectId,
  filter?}`. v1 supports exactly one source: `dataset:
  "chat_messages"` with `objectId` = a chat object in the agent
  space. Any other dataset parses fine and is stamped
  `invalid_spec`-style by the health pass under a new marker
  `unsupported_source` (the `unsupported_kind` marker retires with
  this ADR). `filter` is reserved, ignored in v1.
- **Delivery**: the owning device maintains one SSE chat watch per
  distinct `objectId` across its enabled event triggers, alongside
  the ticker. A new
  message fires the trigger's program with
  `args ∪ {"event": {"space", "objectId", "messageId", "text",
  "agent"?}}`. Self-authored messages (the agent's own name — the
  ADR-009 §8 name-scoped rule) never fire.
- **Missed-occurrence rule**: live-only, mirroring cron — a message
  that arrives while the owner is down or disconnected does not fire
  the trigger (no replay, no backlog). The one exception is the chat
  responder below, which keeps its ADR-009 §8 snapshot-backlog
  semantics because answering the user's stale message late is the
  point.
- **Bookkeeping**: each fire is a normal run — run record, rollup,
  circuit breaker, limits. A hot chat wearing the breaker out is the
  breaker doing its job.

### 3. The chat responder is a trigger record

Serve boot seeds one reserved record `chat-watch` (display name "Chat
responder") on the trigger anchor: `kind: "event"`, `spec: {dataset:
"chat_messages", objectId: <general chat>}`, `enabled: true`, owner
unassigned — claimed and stamped by the active bao like any record,
so today's behavior is the default. It is a member of the
election-following set (§1): takeover re-stamps it to the new winner
alongside the standing built-ins, so switching the active device
still moves where bao answers; repinning it in Scheduled overrides
that explicitly. Unlike the standing built-ins it IS record-owned:
reconcile applies to it (enabled edits, repin, eviction protection:
boot re-seeds it if deleted).

- **Dispatch is native, not a program run**: `program:
  "internal:chat-watch"` is informational; the runtime recognizes the
  reserved id and routes fires through the existing watcher
  (`start_or_inject` — dedup seen-set, inject-into-live-conversation,
  deferred-while-not-ready backlog, foreign-agent attribution, and
  the reconnect snapshot backlog of ADR-009 §8 all unchanged). No
  per-message run records — conversations are already logged as
  `agent_turns`; the record's rollup counts conversation STARTS.
- **Election's scope** (ADR-015 §3 restated): claiming unassigned
  records, re-stamping the election-following set on takeover
  (standing built-ins + `chat-watch`), and the deferred-conversation
  drain. The chat watch connects iff this device OWNS `chat-watch`;
  the `active` gate no longer addresses chat directly.
- **Disabled/`enabled: false` on `chat-watch`** = bao stops answering
  chat everywhere — legal, loud in the UI (it is a pause like any
  other), and reversible.
- **Pinned-to-offline-device** = nobody answers chat, by design (the
  pin contract). The UI's device row is the mitigation: Scheduled
  shows the pin next to a "Chat responder" row users actually look at
  when bao goes quiet.

### 4. Out of scope (v1)

Event creation UI (records come from bao/programs; Scheduled renders
and manages them), non-chat dataset sources, `filter`, cross-space
sources, and any liveness-based failover for pinned triggers (a
deliberate non-goal: pins are the substrate for remote runners, and
consensus without a coordinator is not on offer).

## Sequencing

1. Owner vocabulary (§1) — ADR-006 §4 wording + the Scheduled
   "Unassigned" label; no reconcile change (the shipped behavior IS
   Model A).
2. Event engine (§2) — subscription manager keyed off the registry,
   dispatch, health-pass markers.
3. `chat-watch` record (§3) — seed, native dispatch route, election
   re-scope, ADR-009 §8 + ADR-015 §3 amendments; Scheduled shows the
   row (spec rendering: "On new message in <chat>").

## Consequences

- "Where does bao answer" becomes user-visible, repinnable state in
  Scheduled instead of an invisible election verdict; the election
  remains the default assignee and the mover of the "this is bao"
  set, never of user triggers.
- Every background behavior of serve is now one mechanism (a trigger
  with an owner) — remote runners inherit all of it by holding pins.
- The overlap-window double-fire risk stays confined to the
  election-following set (standing built-ins + `chat-watch`) and the
  claim of a brand-new record; user triggers, once claimed, are
  immune — the local-first payoff of Model A.
- A deleted-then-reseeded `chat-watch` id relies on the tombstone
  exception for reserved seeds — serve re-seeds under a generation
  suffix if the bare id is tombstoned (`chat-watch-g2`, reserved-id
  match by prefix).
