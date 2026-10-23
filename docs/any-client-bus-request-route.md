# any server: `POST /v1/events/request` — correlated request/reply on the event bus

Ticket body for Linear (SYN). Companion to anybao ADR-034 (draft,
`docs/adr/034-client-bus.md`). Drafted 2026-10-22.

## Problem

The event bus (doc 21) is fire-and-forget. A client that wants an
answer from another client has to: subscribe to the reply type and
target, wait for `ready`, publish the request, then read until the
correlated reply or a deadline. Every client re-implements this, and
some cannot do it at all:

- The anybao guest runs effects one at a time; an `effect()` call
  blocks until its HTTP exchange ends, and a subscription stream does
  not end. anybao PR #110 adds ~290 lines of Rust and its own SSE
  reader to do the interleaving in the host, for one event family.
- any-ui would need the same for any UI-to-bao or window-to-window
  question.
- Across devices the asker's pub/sub interest must have propagated
  through the sync nodes before the receiver publishes its reply.
  Only the server sees when that is true.

The server owns the hub (`events_hub.go`) and the SDK pub/sub bridge
(`events_bridge.go`). It is the one place that can do this once.

## Proposal

One blocking route beside `POST /v1/events`:

```
POST /v1/events/request
```

Body:

```jsonc
{
  "type":    "ui.theme.select",        // doc 21 type grammar
  "scope":   "account" | "device" | "space",
  "spaceId": "…",                      // iff scope == "space"
  "to":      "<clientId>",             // optional; absent = broadcast
  "data":    { … },                    // ≤ 64 KiB, feature payload
  "timeout": 5                         // seconds, (0, 30], default 5
}
```

Server behaviour, in order:

1. Mint `requestId` (uuid v4). Compute `expiresAt = now + timeout`.
2. Subscribe locally on `type = "<type>.reply"`, `target =
   requestId`, same scope and `spaceId`. On a network scope this
   registers the pub/sub interest; wait until the bridge confirms the
   SDK subscription is live before step 3.
3. Publish the request envelope: `type`, `scope`, `spaceId`,
   `target = requestId`, `data` = the caller's data plus a reserved
   `request` key `{id, from, to, expiresAt}`. `from` is the server's
   own client id for the caller (see "Open points"). Reject a caller
   `data.request` with `400 request.unknown_field`, same rule as
   `sender`.
4. If the publish matched zero subscribers (hub count, the existing
   `subscribers` number), return at once with `outcome:
   "no_consumers"`. Nothing was asked; a retry is safe.
5. Otherwise wait. With `to` set, return on the first reply whose
   `data.reply.id == requestId` and `data.reply.from == to`. Without
   `to`, collect every correlated reply until the deadline.
6. On the deadline return `outcome: "timeout"` with the replies so
   far (empty for a targeted request). Never republish.
7. Drop the subscription.

Response, `200`:

```jsonc
{
  "requestId": "…",
  "outcome":   "replied" | "timeout" | "no_consumers",
  "replies":   [ <reply envelopes, verbatim, sender stamped> ]
}
```

Reply envelope the receiver publishes with plain `POST /v1/events`:

```jsonc
{
  "type":   "ui.theme.select.reply",
  "scope":  "account",
  "target": "<requestId>",
  "data":   { "reply": { "id": "<requestId>", "from": "<clientId>", "ok": true }, … }
}
```

A reply with `sender.self == false` is not correlated (dropped for
this route; it still fans out to ordinary subscribers). A reply whose
`data.reply.id` is not the target is ignored.

## Validation (`400`)

- All of `POST /v1/events`' rules on `type`, `scope`, `spaceId`,
  `data` size and topic budget.
- `request.invalid_field`: `timeout` not a finite number in `(0, 30]`;
  `to` not matching `[A-Za-z0-9._/-]{1,160}`.
- `request.unknown_field`: `data.request` or `sender` supplied.
- Network-scope errors as for publish (`409 events.no_read_key`, …).

Cancellation: the client closing the connection drops the wait and
the subscription. The request, if already published, stands; the
route does not undo anything.

## Limits

- Concurrent requests per connection/account: a small cap (e.g. 32),
  `429 events.too_many_requests` beyond it. Each one holds an SSE-less
  hub subscription for ≤ 30 s.
- The `.reply` type adds one segment to the topic; the 256 byte / 16
  segment topic budget is checked on the reply type too.
- Interest patterns: a targeted request registers an exact pattern
  `ev/<type>/reply/<requestId>`; the 100-patterns-per-space cap
  applies, which is why the concurrency cap matters.

## Acceptance

- Same device, two local subscribers: a targeted request returns
  `replied` with one envelope in < 50 ms; a broadcast collects two
  replies and returns at the deadline.
- Two devices on the staging network: a request from device A to a
  client on device B returns `replied`; `sender.self` is true on the
  reply; a reply published by B before A's interest existed is not
  lost (step 2's wait proves itself).
- No matching subscriber: `no_consumers`, no wait.
- Receiver silent: `timeout` at `timeout` seconds ± 100 ms.
- `data.request` in the body: `400 request.unknown_field`.
- `any events request` CLI verb mirrors the route (optional, for
  smoke tests).

## Open points

- **`from`.** The server knows its own peer id but not which local
  client (window) is asking. Either the caller passes `from` and the
  server copies it into `data.request.from`, or `from` is the peer
  id alone. Passing it is simpler and matches how `to` works.
- **Reply type.** `<type>.reply` as above, or a single `bus.reply`
  type filtered by `target`. The route hides this from callers;
  decide by what keeps topic interests cheap.
- **Docs.** New section in doc 21 ("Request/reply"), `api-parity.md`
  fingerprint for the route so anybao's drift test sees it.
