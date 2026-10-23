# ADR-034: Client bus — presence, directives and request/reply between an account's clients over the event bus

Status: **Draft** (2026-10-22, not yet in review)
Date: 2026-10-22
Builds on: any doc 21 (event bus: envelope, scopes, topic mapping,
`sender`), ADR-025 (bao presence beats — the first participant),
ADR-005 §5 (the view rides the message), ADR-002 (effect boundary),
ADR-008 (agent tools), ADR-010 §4 (`__any_tool__`)
Amends (when accepted): ADR-025 §1/§6 (the beat becomes one profile
of a generic presence beat), ADR-008 §8 as proposed in anybao PR #110
(theme control becomes two request types on this bus, the `ui.theme`
host effect is not needed), `any@v1.open_in_ui` (a directive on this
bus)
Tracks: anybao PR #110 + any-ui PR #2531 (theme control, the case
that surfaced the gap); the any server ticket
`docs/any-client-bus-request-route.md` (the correlation route)

## Context

Three features talk between clients of one account today, and each
invented its own shape on the same transport:

| feature | pattern | publisher | scope | correlation |
|---|---|---|---|---|
| `bao.status` (ADR-025) | one-way full-state beat, TTL | serve | account | none |
| `ui.open_space` / `ui.open_object` | one-way directive | guest, `http.post` | device | none |
| `ui.theme.request` / `ui.theme.result` (PR #110) | request/reply | host effect `ui.theme` | device | `target` = request id |

The transport is already the right one. The any server's event bus
(doc 21) is account-wide: `device` scope stays on the local hub,
`account` and `space` scopes ride any-sync's ephemeral pub/sub to
every online device, read-key encrypted and signed per message. New
event types need no server change.

What is missing sits one layer up:

1. **Nobody knows who is on the bus.** PR #110 discovers UI windows
   with a broadcast `capabilities` request and a one-second wait. The
   UI learns about bao only through `bao.status`. There is no general
   "I am a client, I answer these requests, I show this" signal.
2. **Request/reply has no home.** A correlated exchange needs the
   reply subscription open before the request is published. A guest
   `effect()` call blocks until its HTTP exchange ends, and a
   subscription stream does not end, so the guest cannot interleave
   the two (PR #110's reason for 290 lines of Rust). Every client that
   wants to ask another client a question would repeat that dance.
3. **Conventions differ per feature.** Request ids, deadlines,
   targeting a window, what a reply looks like, what a timeout
   means — PR #110 defines all of them well, but for themes only.

The user's framing: one common bus for status, the current object,
theme changes, "what do you see", and any future question one client
asks another, on the same device or not.

## Decision

### 1. Participants and presence

Every client of the account that wants to be addressed beats a
`client.presence` event at `account` scope, same cadence and
staleness rule as ADR-025 §2 (every 10 s, offline after 3 missed
beats, one `shutdown` beat on graceful exit):

```jsonc
{
  "type":   "client.presence",
  "scope":  "account",
  "target": "<clientId>",                 // filterable, the dedup key
  "data": {
    "client": {
      "id":     "<clientId>",             // account-unique, see §3
      "kind":   "ui" | "serve" | "cli",
      "device": "<peer id>",              // ADR-015 identity
      "name":   "MacBook — window 2",     // for people
      "answers": ["ui.theme.*", "ui.view.get"],   // request types it serves (doc 21 type grammar, `.*` prefix ok)
      "state":  { … }                     // kind-specific, full-state, last-write-wins
    }
  }
}
```

`state` is where ADR-025's layers go for `kind: serve` (`state`,
`role`, `winner`, `run`, `line`, `lineAt` — unchanged fields). For
`kind: ui` it carries the window's `view` (`{spaceId, objectId?,
view}`, the same group any-ui stamps on a chat message) and
`active`/`focused`. A beat is idempotent and full-state, so a dropped
one costs nothing.

**This does not replace ADR-005 §5.** The view on the message stays
the record of where the user was when they spoke; it is durable and
replays with the trace. The view in a beat is "where they are now",
for a mid-run question, and it is as transient as the bus.

Discovery is a read of the presence map, not a round trip: a
consumer (any-ui, serve, the guest through serve) keeps the last beat
per `clientId` and ages it out. PR #110's `capabilities` request
becomes unnecessary.

### 2. Three message shapes, one envelope

All three use doc 21's envelope unchanged. The bus metadata lives
under reserved keys in `data`; everything else in `data` belongs to
the feature.

**Beat** — §1. `target` = client id.

**Directive** — one-way, fire-and-forget, no reply expected.
`ui.open_object` is one. `data.to` names the client (§3) or is
absent for "whoever handles it" (then the receiver's own gate
decides, e.g. any-ui's active-window rule). `subscribers: 0` on
publish means nobody local was listening; it is not an error.

**Request** — expects exactly one reply per addressed client:

```jsonc
{
  "type":   "ui.theme.select",            // the verb; the family prefix is what `answers` lists
  "scope":  "account",
  "target": "<requestId>",                // uuid; replies carry the same target
  "data": {
    "request": { "id": "<requestId>", "from": "<clientId>", "to": "<clientId>" | null,
                 "expiresAt": 1761130000.0 },
    …feature params…
  }
}
```

**Reply** — `type` = request type + `.reply`, `target` = the request
id:

```jsonc
{
  "type":   "ui.theme.select.reply",
  "scope":  "account",
  "target": "<requestId>",
  "data": {
    "reply": { "id": "<requestId>", "from": "<clientId>", "ok": true },
    …feature result, or "error": {"code", "message"} when ok is false…
  }
}
```

`to: null` is a broadcast request; every client whose `answers`
covers the type replies, and the asker collects until the deadline.
A client that receives a request it does not answer stays silent. A
request past `expiresAt` is dropped unanswered on the receiving side
(a stale preview must never apply).

### 3. Addressing and scope

A `clientId` must be unique across the account, because clients on
different devices share the bus. Form: `<peer id>/<local id>`, where
the peer id is the device identity ADR-015 already uses and the local
id is the window's or process's own uuid. A client mints it once per
process lifetime; a UI reload is a new client.

`scope` is `account` for everything on this bus. "This device only"
is expressed by `to`, not by `device` scope — the asker knows the
receiver's `device` from presence. `device` scope remains available
for signals that must never leave the machine, but no client-bus
shape depends on it.

Trust: `sender.identity` and `sender.self` are stamped by the server
from the pub/sub signature (doc 21), and `self` is true on every
device of the publishing account. A receiver accepts requests and
replies only when `sender.self` is true. `data.request.from` and
`data.client.id` are claims within the account, not proof of
identity; that is enough for one account's own devices. Making
`client.presence` a self-owned type (doc 21 `selfOwnedEventTypes`)
would make beats spoof-proof across accounts in a shared space; it is
a coordinated server change and is not required for v1.

### 4. Correlation lives in the any server

A request/reply exchange needs: subscribe to the reply type and
target, wait for `ready`, publish, wait for the reply or the
deadline. Every client needs it, the guest cannot do it at all, and
across devices it also needs the pub/sub interest to have propagated
before the receiver publishes. The server owns the hub and the SDK
pub/sub, so it is the one place that can do this once for everyone.

One blocking route, specified in `docs/any-client-bus-request-route.md`:

```
POST /v1/events/request
{ "type": "ui.theme.select", "scope": "account", "spaceId"?: "…",
  "to"?: "<clientId>", "data": {…}, "timeout": 5 }
→ 200 { "requestId": "…", "replies": [ <reply envelopes> ], "outcome": "replied" | "timeout" | "no_consumers" }
```

The server mints the request id, writes `data.request`, subscribes
first, publishes, and returns when the addressed client has replied
(or for a broadcast, at the deadline with every reply so far). It
never retries and never republishes. `outcome: timeout` after a
publish is an unknown outcome for the caller; `no_consumers` means the
publish matched zero subscribers, so nothing was asked.

Until the route exists, a client that needs request/reply may do the
dance itself (PR #110's host effect is that, for one family). Such
code is a dated bridge with the ticket referenced, per the house rule
on any-server quirks.

### 5. The guest surface — `bus@v1`

One small space-resident program, `__any_tool__`, three verbs, all
over the existing `http.*` effects against the configured any server.
No new host effect, no new Rust.

- `clients(kind=None, answers=None)` — the presence map as serve sees
  it (§6), filtered. Rows are `data.client` plus `lastSeen` and
  `stale`.
- `tell(type, data, to=None)` — a directive: one `http.post` to
  `/v1/events`, returns `{subscribers}`.
- `ask(type, data, to=None, timeout=5)` — one `http.post` to
  `/v1/events/request`, returns the route's response unchanged.

Classification for the trace and capabilities (ADR-002 §2): a verb
whose type ends in `.get`, `.list`, `.catalogue` or `.capabilities`
is a read; everything else is a mutate. The rule is on the type
suffix, written once in `bus@v1`, not a per-family match arm in the
broker. The host's `http.*` allowlist already bounds where the guest
may post; a type-prefix allowlist for `ask`/`tell` (`ui.*`,
`client.*`) is the capability grant, so bao cannot invent event
families the UI does not expect.

Feature programs sit on top and own their docstrings and skill text:
`themes@v1` from PR #110 keeps its 44 lines and its skill, and its
two functions become `bus.clients(kind="ui", answers="ui.theme.*")`
and `bus.ask("ui.theme." + action, params, to=client_id)`.
`any@v1.open_in_ui` becomes `bus.tell("ui.open_object", {...},
to=…)`, with `to` resolved from presence instead of any-ui's
active-window gate when the caller names a client.

### 6. Serve as a participant

Serve beats `client.presence` with `kind: serve`, replacing the
`bao.status` publisher (ADR-025 §1 amended: same fields, under
`data.client.state`; the type changes). Serve also keeps one standing
subscription on `client.presence` for the whole process and holds
the presence map; the guest reads it through a `runtime.get`-style
read (`bus.clients`), so a run never waits on the bus to learn who is
there. Serve answers requests addressed to it in `answers`:
`bao.status.get` (the current beat on demand) and `bao.break` (the
hard break of ADR-005 §3, today a chat control record) are the first
two.

any-ui, per window: beat with `kind: ui`, `view`, `active`, `focused`
and `answers` (the theme family from any-ui #2531, `ui.view.get`,
`ui.open_object` as a directive it handles); keep the presence map
for the status bar (the bao dot reads the `serve` rows); dispatch
requests by type to handlers that return the reply payload; the bus
layer writes the reply envelope. Navigation keeps the active-window
gate for directives without `to`.

### 7. Failure semantics

- A directive has no outcome. The caller may read state afterwards.
- A request with `outcome: replied` and `reply.ok: true` is the only
  success. `ok: false` is the receiver's error, passed through.
- `timeout` after a publish is unknown. The caller reads state
  (`*.get`) or presence before deciding; mutating requests are never
  retried automatically. `no_consumers` means nothing was asked and a
  retry is safe.
- An offline client misses everything. The bus never queues. "Apply
  when it comes back" is a synced record, not this bus.
- Rate: doc 21 allows 30 msg/s per peer. Beats every 10 s per window
  and request/reply at human pace are far under it.

### 8. Testing

- `bus@v1` guest tests against a scripted `http.*` effect: shapes,
  classification by suffix, `ask` passes the route response through
  unchanged.
- Serve: presence map ages rows out; `client.presence` beat carries
  ADR-025's fields under `state`; the two served requests reply with
  the right `target`.
- Live, once the route lands: two UI windows on two devices on the
  staging network; `clients()` lists both with their views; a theme
  preview addressed to the remote window applies there and nowhere
  else; a request to a closed window returns `timeout`; a request on
  a type nobody answers returns `no_consumers`.

## Consequences

- One convention replaces three. New client-to-client features are a
  type family, a handler in the UI, and a feature program; no Rust,
  no server change beyond the one route.
- PR #110 shrinks to its program, skill and ADR text. `ui_theme.rs`
  and its tests go away; the any-ui receiver in #2531 becomes one
  handler family behind a generic dispatcher.
- Cross-device works by default. The mac serve can drive a UI window
  on the laptop, and the laptop UI sees the mac's bao.
- The bus stays ephemeral. Nothing here persists, replays or
  accumulates in a space.
- Cost: a UI window now publishes a beat every 10 s on the network
  scope; the pub/sub relay must carry `pubsubrelay` (any-sync-node ≥
  v0.13.1) or beats reach only LAN peers.

## Open questions

1. **Reply type suffix vs. one reply type.** `<type>.reply` keeps
   topic filters per family. A single `bus.reply` type would need
   filtering by `target` only, which doc 21 also supports. The route
   hides the choice from clients; pick on the server side.
2. **`bao.status` compatibility.** Keep publishing `bao.status`
   beside `client.presence` for one release so an older any-ui keeps
   its dot, then drop it. Or make the UI read both from day one and
   never dual-publish.
3. **Self-owned presence.** Whether `client.presence` goes into the
   server's `selfOwnedEventTypes` registry. Not needed within one
   account; needed if presence is ever shown to other members of a
   shared space.
4. **Should the view in the beat be opt-out?** It tells every device
   of the account what every window shows. Same account, so the data
   is already theirs, but a settings toggle may still be wanted.
