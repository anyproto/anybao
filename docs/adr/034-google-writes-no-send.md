# ADR-034: Google writes without a send path — Gmail drafts and calendar holds behind a host-enforced write allowlist

Status: **Proposed** (2026-11-09; draft PR — merge is acceptance)
Date: 2026-11-09
Builds on: ADR-002 (the effect boundary: nothing executes side effects
except through it), ADR-011 §2 (provider descriptors, host-only data),
§4 (the redirect rule), §6 (host-emitted records and their replay
drain), §7 (the Google family), ADR-021 §7 (host binding at hop zero),
ADR-033 §5.1 (the brain cannot send)
Amends: ADR-011 §2 (a descriptor gains `guest_writes`), ADR-011 §7 (the
family is no longer read-only; writes get their own provider handle;
Google no longer folds earlier grants into a new token),
ADR-033 §5.1 (why Gmail drafts were not used, and what now holds the
no-send property)
Tracks: Linear GO-7602 "Bao: write Gmail drafts and Calendar holds
without a send path"

## Context

Bao reads the user's Gmail and Google Calendar (`gmail.readonly`,
`calendar.readonly`, ADR-011 §7) and can write to neither. The ask is
the obvious next step: Bao prepares email drafts in the user's Gmail
(the user reviews them and presses Send) and puts things on the user's
calendar.

Two facts make this more than adding scopes.

**Google's scopes are coarse.** No scope writes drafts without also
sending: `gmail.compose` covers `drafts.*`, `messages.send` and
`drafts.send`. `calendar.events` covers inserting an event with
attendees (Google emails them an invitation), moving it to another
calendar, and editing or deleting any event, including meetings other
people organise.

**Bao reads untrusted text.** Mail, web pages and files reach the
model, and a cell can call any effect the run allows. ADR-033 §5 states
the invariant: *whatever reads untrusted mail cannot also send*.
§5.1 holds it by scope: Google access stays `gmail.readonly`, and
drafts are not used because the scope that writes them can also send.

That fence is weaker than it reads. `googleAuth.connect(scopes=[…])`
takes a guest-chosen scope list (ADR-011 §5). A cell can ask for
`gmail.send` or `https://mail.google.com/`, and one click on Google's
consent screen turns the read-only family into a token that sends,
reachable from every cell. Today the invariant rests on the human
reading a consent screen.

The broker already sees every credentialed request — method, URL,
headers and body — after the effect is recorded and before anything is
sent. ADR-021 §7 enforces host binding at exactly that point. The fence
belongs there.

## Decision

### 1. The invariant

A request that carries a managed Google token may change the account
only through the writes the provider's descriptor lists in
`guest_writes` (§2). None of those writes sends mail, invites a person,
shares anything, or touches an item Bao did not create. Reads (`GET`,
`HEAD`) are unaffected.

The list is host-only data embedded in the binary (ADR-011 §2: never
merged into guest-readable config), so no guest code, overlay or
config row can widen it. It holds whatever scopes the grant carries:
a token with `gmail.send` or full `https://mail.google.com/` access is
bound by the same list.

Because the guard lives in the binary, a write-capable token must
never reach a binary without it. Grants are account-scoped and sync to
every device (ADR-011 §3, amended), and a device may run an older
anyrt. So writes get **their own provider handle**,
`connector.oauth.google_write`, with its own consent and its own
refresh token (§2, §6). An older runtime has no `google_write`
descriptor: it cannot run that consent (`not_configured`), and the
synced refresh token is a sub-ref it can never inject (ADR-011 §4). The
read handle's token no longer picks up write scopes by folding (§6). A
cell can still name a write scope on the read handle
(`connect(scopes=[…])`), which is the gap from the Context, not a new
one. On this runtime the read handle's table bounds it to the sync's
batch. A rollout in either order therefore fails closed.

### 2. `guest_writes` on a provider descriptor (amends ADR-011 §2)

ADR-011 §2 kept injection metadata out of the descriptor and deferred
it to host binding. Host binding landed in ADR-021 §7, keyed on the
credential row's `hosts`, which come from the connector's
`__any_credentials__` manifest — guest-authored data. A write
allowlist must not come from the guest, so it lives on the descriptor.

- `guest_writes` absent: no write policy. Other providers and every
  static ref (`connector.key.*`) behave as before.
- `guest_writes` present, even empty: every request through the
  provider's handle whose method is not `GET` or `HEAD` must match one
  rule. Otherwise the effect fails typed `credential_write_refused`,
  naming the method and path.

A rule:

| field | meaning |
|---|---|
| `method` | the HTTP method, exact |
| `path` | a template: literal segments; `*` is one segment; `prefix*` is one segment that starts with `prefix` |
| `query` | name → value: the parameter appears exactly once with exactly this value; the write carries no other parameter |
| `check` | a named body check implemented in the host (§4, §5) |
| `preflight` | a named ownership read the host performs before the write (§4, §5) |

`check` and `preflight` are names, not expressions. The descriptor
chooses which ones apply. The host code implements them, because the
rules (a MIME header block, a multipart batch, an event's attendees)
are not worth a policy language. An unknown name fails the descriptor
parse, which tests run.

The Google tables. The read handle `google` writes only the sync's
batch:

```json
"guest_writes": [
  {"method": "POST", "path": "/batch/gmail/v1", "check": "gmail_batch_reads"}
]
```

The write handle `google_write` (same endpoints, its own scopes, §6):

```json
"guest_writes": [
  {"method": "POST",   "path": "/gmail/v1/users/me/drafts", "check": "gmail_draft_text"},
  {"method": "PUT",    "path": "/gmail/v1/users/me/drafts/*",
   "check": "gmail_draft_text", "preflight": "gmail_draft_owned"},
  {"method": "DELETE", "path": "/gmail/v1/users/me/drafts/*", "preflight": "gmail_draft_owned"},
  {"method": "POST",   "path": "/calendar/v3/calendars/primary/events",
   "query": {"sendUpdates": "none"},
   "check": "calendar_hold_insert"},
  {"method": "PATCH",  "path": "/calendar/v3/calendars/primary/events/baoagent*",
   "query": {"sendUpdates": "none"},
   "check": "calendar_hold_patch", "preflight": "calendar_event_solo"},
  {"method": "DELETE", "path": "/calendar/v3/calendars/primary/events/baoagent*",
   "query": {"sendUpdates": "none"},
   "preflight": "calendar_event_solo"}
]
```

### 3. Matching is fail-closed

The checker's job is to approve only what it fully understands. A
request it cannot parse is refused. So is a request that Google might
read differently from the checker.

- **The URL is parsed by the parser the HTTP client sends with** (the
  `url` crate, which ureq uses). Dot segments are resolved before
  matching, so the path that is checked is the path that is sent.
- **Literal segments compare byte-exact**: case-sensitive and still
  percent-encoded. `/Drafts`, `/drafts%2Fsend` and `/drafts;x` match
  nothing.
- **Wildcard segments are percent-decoded**, then must be 1–1024
  characters from `A–Z a–z 0–9 - . _ @ +`, and not `.` or `..`. A
  segment that decodes to `/`, `:` or whitespace matches nothing.
- **Query names compare normalised**: ASCII-lowercased, with every
  non-alphanumeric character removed (`send_updates` = `sendUpdates`).
  Google's front end accepts aliases, so a required parameter must
  appear exactly once under any spelling. A write carries no parameter
  its rule does not name (`sendNotifications`, `supportsAttachments`,
  `conferenceDataVersion` all refuse).
- **Some headers are the host's.** A cell may not set `Host`,
  `Authorization`, `Proxy-Authorization`, `Cookie`, `Content-Length`,
  `Content-Encoding`, `Transfer-Encoding`, `Connection`, `Upgrade`,
  `TE` or `Trailer` on any request through the handle, and no
  `Content-Type` beside `json=`. A cell's `Host` would route the token
  past the path rules, and ureq sends a cell's lower-case
  `authorization` or `content-type` next to the host's own rather than
  replacing it.
- **Method tunnels are refused on every request through the handle**,
  reads included: a header or query parameter whose normalised name
  contains `method` (`X-HTTP-Method-Override` and kin) fails the
  request — and so does one in a batch's inner request line or inner
  headers (§4). Those move a write past a method check.
- **Bodies the checks read must be `json=`** (the broker sends their
  canonical serialisation, so a duplicate key cannot exist on the
  wire) — except the batch, which is a `body=` text with its
  multipart content type.
- **Every redirect hop is re-checked.** ADR-011 §4 follows credentialed
  redirects only same-origin, but a 307/308 re-sends the body. A hop
  that would need a preflight is refused.
- **Order**: after the effect is recorded and before the token is
  resolved. A refusal costs no refresh and replays from the trace like
  any failure (ADR-021 §7).

### 4. Gmail: drafts, and the sync's batch reads

- **`gmail_batch_reads`** keeps gmailSync's hydration working (ADR-012
  §2: 25 `messages.get` per multipart batch). A batch can wrap any
  request, including a send, so the body is parsed strictly. The
  boundary comes from the request's `Content-Type`
  (`multipart/mixed`). Line endings are CRLF only. The preamble is
  empty, the close delimiter is present, and at most one CRLF follows
  it. Every part carries only `Content-Type: application/http` and an
  optional `Content-ID` (a `Content-Transfer-Encoding` would hide the
  inner request from the parser), and holds exactly one inner request:
  `GET /gmail/v1/users/me/…`, optionally followed by ` HTTP/1.1`. An
  inner request has no body and no method tunnel, in its query or its
  headers. Anything else in the body refuses the whole batch.
- **`gmail_draft_text`** reads the `drafts.create` / `drafts.update`
  body. Top-level keys are a subset of `{id, message}`, and
  `message`'s keys a subset of `{raw, threadId}`. A create carries no
  `id`; an update's `id`, when present, is the one in its path. `raw`
  decodes (base64url or base64, padding optional) to a message whose
  header block parses strictly: printable ASCII and tab only (no NUL,
  no lone CR), every line a header or a continuation. The type is `text/plain` (an absent `Content-Type`
  means `text/plain`; two of them refuse). The message carries the
  marker header `X-Anybao-Draft`.
  Why `text/plain`: an HTML draft can embed remote images, which load
  when the user opens the draft. That is an exfiltration channel that
  needs no send.
- **`gmail_draft_owned`** guards update and delete. Before the write,
  the host reads `drafts/<id>?format=metadata&metadataHeaders=X-Anybao-Draft`
  with the same credential, as a host-emitted `http.get` record
  (`meta.hosted`, drained on replay — the ADR-011 §6 mechanism).
  Host-emitted calls never consult a mock: a cell-authored span mock
  (ADR-028) cannot answer the ownership read. The write proceeds only
  when the draft carries the marker. A read that answers 300 or more
  (a rate limit, an expired token, a draft that is gone) is returned as
  the write's own answer, marked `preflight: true`, and the write is
  not sent; the caller handles that status as it would its own. Gmail deletes
  drafts permanently, so without this a cell could destroy or overwrite
  a draft the user wrote. A draft the user re-saves in Gmail is
  expected to lose the marker and with it Bao's access (to verify
  live, §8).
- **Refused because unlisted**: `messages.send`, `drafts.send`, every
  `/upload/…` variant, `messages.insert`/`import`/`modify`/`trash`/
  `delete`, labels, filters, forwarding, `sendAs`, delegates, vacation
  replies, `watch`, every other `users/<id>` than `me`, and the global
  `/batch` endpoint.

### 5. Calendar: private holds on the primary calendar

Bao's calendar writes are **holds**: events on the user's primary
calendar that only the user and the people who can edit that calendar
see, and that no one is invited to.

- **`primary` only.** A calendar the user can write to is not one only
  the user reads. Anyone can share a writable calendar with the user,
  and a hold written there is a message to its owner.
- **`sendUpdates=none` on every write; `sendNotifications` never.**
  Google sends no email about the write.
- **Bodies are allowlists.** A hold carries only `id`, `summary`,
  `description`, `location`, `start`, `end` (`date` / `dateTime` /
  `timeZone`), `visibility` (`"private"`), `transparency`, `colorId`,
  `recurrence` (strings), `extendedProperties` (`private` only, string
  values) and `eventType` (`"default"` only). Everything else refuses
  the body, because Google acts on other people for keys a blocklist
  would miss. `attendees` invites people. `eventType: "outOfOffice"` /
  `"focusTime"` with `autoDeclineMode` declines the user's existing
  meetings and sends their organisers a cell-chosen message.
  `reminders` can email. `conferenceData` creates a meeting room.
- **`calendar_hold_insert`**: the allowlist above, plus an `id` that
  starts with `baoagent` and is base32hex (`0-9a-v`, 5–1024 characters
  overall — `events.insert` lets the client choose an id) and
  `visibility: "private"`, so a calendar shared with "see all event
  details" shows only "busy".
- **`calendar_hold_patch`**: the same allowlist without `id`, plus
  `status: "confirmed"` (restoring a hold Google keeps as cancelled
  after a delete). Patch, not update: omitted fields stay.
- **The `baoagent` prefix in the path** limits patch and delete to
  events Bao created. The user's own events and other people's meetings
  carry Google-generated ids. An attacker can choose an id for an event
  they organise and invite the user, but the preflight below refuses
  that event (it lists the organiser).
- **`calendar_event_solo`** guards patch and delete. Before the write,
  the host reads the event (a hosted `http.get`, as in §4). The write
  proceeds only when every listed attendee is the user (`self: true`)
  and the organiser, when listed, is the user. A hold the user has
  turned into a meeting by adding guests becomes theirs: Bao's edit
  would reach the guests' calendars even with `sendUpdates=none`.
- **Refused because unlisted**: `quickAdd` (it parses attendees out of
  text), `import`, `move`, `watch`, `instances` other than by id,
  `acl`, `calendars`/`calendarList` writes, settings, and every
  calendar other than `primary`.

Bao cannot invite anyone. To set up a meeting, Bao proposes a time,
keeps a hold, and drafts the invitation email (§4). The user sends it
or adds guests in Google Calendar.

Drive, Sheets and Meet get no rules, so they stay read-only by policy
as well as by scope.

### 6. Scopes: a second consent, and no folding (amends ADR-011 §7)

- **Two handles, two consents.** `googleAuth.connect()` is unchanged:
  the read handle `google`, the read-only default union.
  `googleAuth.connect(writes=True)` runs a separate consent for the
  write handle `google_write`, whose scopes (`openid`, `email`,
  `gmail.compose`, `calendar.events`) live only in the host table; it
  takes no guest scope list. Both use the same shipped OAuth client.
- **No folding.** Both descriptors set `include_granted_scopes=false`.
  With `true`, Google adds every scope the user ever granted the client
  to each new token, so a later read re-consent would mint a read token
  that can compose, which an older runtime would inject unguarded (§1).
  Each token now carries exactly what its consent asked for.
- **Partial read lists still work.** Without folding, a guest scope
  list replaces the default, so `googleAuth.connect(scopes=[…])` now
  sends the read union plus the extra scopes (googleDrive's
  `drive.readonly` re-consent keeps Gmail). The guest's copy of the
  union is pinned to `oauth_providers.json` by a test.
- **Errors say what to do.** A write without the write grant fails
  `not_connected` on `google_write`, and the connectors say to run
  `googleAuth.connect(writes=True)` in a user-facing turn. A runtime
  without the descriptor (`no oauth provider "google_write"`) gets
  "this runtime predates the write guard — update the app". A 403 on a
  write (the user unticked a scope on Google's granular consent screen)
  points at the same `connect(writes=True)`.
- **One disconnect.** `googleAuth.disconnect()` revokes and deletes the
  write grant, then the read grant. Google may end every token of the
  client at the first revoke, so a partial disconnect is not offered;
  to stop drafts and holds only, disconnect and run `connect()` again.

Operational prerequisite, the owner's: the shipped OAuth client's
consent screen in Google Cloud Console must list both scopes, or Google
refuses them. `gmail.compose` is a restricted Gmail scope, as
`gmail.readonly` already is; `calendar.events` is sensitive. The
verification class of the client does not change.

### 7. Connector surface

`gmail@v1` gains `list_drafts`, `get_draft`, `create_draft`,
`update_draft`, `delete_draft` and `get_profile` (the user's own
address):

- `create_draft(to, subject, body, cc?, bcc?, reply_to?, reply_all?)`
  builds a `text/plain` UTF-8 message with Python's `email` package
  (header folding, encoded words, CR/LF refused in every header) and
  stamps `X-Anybao-Draft`. `reply_to` names a message id. The draft
  then threads in Gmail: its `threadId`, `In-Reply-To` and
  `References` are copied from the original, the subject defaults to
  `Re: <subject>`, and `to` defaults to the original's `Reply-To`,
  else its `From`. `reply_all` adds the original's To and Cc, minus
  the user's own address (from `users.getProfile`); with an explicit
  `to`, the others go on Cc. A `subject` given with `reply_to` must
  match the thread's, ignoring `Re:`/`Fwd:`/`AW:`/`WG:` prefixes, since
  Gmail threads a draft only then. Addresses are ASCII
  (internationalized ones would need SMTPUTF8). Addresses read from
  the original that cannot be used, such as a group like
  `undisclosed-recipients:;`, are skipped.
- `update_draft(draft_id, …)` replaces a draft, keeping every field it
  is not given. `delete_draft(draft_id)` deletes one. Both work on
  Bao's own drafts only (§4); on any other draft they return an error
  saying the draft is the user's.
- Reads use `googleAuth._CRED`; writes use `googleAuth._CRED_WRITE`
  (§1). The runtime's preflight reads go out on the write handle,
  which can read drafts and events.
- A refusal (`credential_write_refused`) is returned as `{ok: False,
  refused: True, error}`, and is never retried.

`googleCalendar@v1` gains `create_event`, `update_event` and
`delete_event`:

- `create_event(summary, start, end, description?, location?,
  time_zone?, key?)` writes a private hold on the primary calendar.
  `start`/`end` are RFC 3339 date-times or `YYYY-MM-DD` dates (all
  day). With `key`, the event id is `baoagent` + the first 32 hex
  characters of `sha256(key)`, so a re-run with the same key returns
  the existing hold (`created: False`) instead of a second one. If
  that hold was deleted, Google answers 409 for the reused id and keeps
  the event as cancelled, so the connector restores it with a patch
  (`status: "confirmed"`).
  Without it, the id hashes the inputs, `now()` and a draw from the
  run's seeded stream (ADR-002 §4), so replay mints the same id.
- `update_event` and `delete_event` take a `baoagent…` id. They return
  an error for other ids before any request; the host refuses them
  anyway.
- No method takes attendees. The docstrings say how to invite.

### 8. Tests and live verification

Runtime unit tests cover the matcher (dot segments, case, percent
encoding, the wildcard charset, query aliases, duplicates and extra
parameters, method tunnels, host-only headers), the batch parser
(accepted, inner `POST`, an inner query tunnel, a part transfer
encoding, LF-only, preamble junk, missing close), the draft check
(HTML, multipart, two content types, a malformed header line, NUL and
control bytes, no marker, extra keys, ids), the calendar allowlists
(out-of-office, email reminders, conference data, unknown keys), both
preflights through the broker against a loopback server, a span mock
that tries to answer an ownership read, a failed read returned as the
answer, the redirect re-check, and the built-in tables (`messages/send`
and `drafts/send` refused, the shipped connectors' requests allowed).
An independent adversarial review (2026-11-09) found the mock bypass,
the calendar blocklist, the `Host` header and the batch and draft
looseness above; each has a regression test.
Connector tests run the new methods through the real guest kernel
(the `kernelenv` harness). Two vectors tie the halves together: a
draft body the connector built under the kernel is a Rust test case
the checker must admit, and gmailSync's batch body is pinned byte for
byte on both sides.

Not yet verified live, the owner's test: that `X-Anybao-Draft` survives
`drafts.create` and is dropped when the user re-saves the draft; that
a reused event id answers 409; that the shipped client's consent screen
grants both scopes.

### 9. Out of scope

- Sending mail. Bao sending by itself (ADR-033 §5.C: the `google_send`
  host-only consent, the `approval_grant` attachment, `outbox.send`)
  stays on any-ui's `docs/plans/2026-10-25-ai-brain-later.md`.
- Inviting attendees, and editing events Bao did not create.
- Labels, archiving and other mailbox changes; Drive and Sheets writes.
- The brain: `brainDrafter@v1` still writes `drafts` records, and the
  hand-off stays any-ui's (ADR-033 §5.2). Mirroring an approved reply
  into a Gmail draft is a follow-up amendment of ADR-033 Part 5.
- Multi-account (ADR-011, resolved Q5).

## Consequences

- Bao can prepare Gmail drafts, threaded as replies, and keep private
  holds on the primary calendar. The user sends, and the user invites.
- "Cannot send" becomes a property of the binary rather than of the
  grant. It now also holds against a token that carries a send scope,
  which closes the existing `connect(scopes=…)` gap.
- ADR-033 §5.1's rationale is superseded: drafts no longer put a send
  capability behind a guest-injectable token, because the token cannot
  reach a send endpoint. The brain is unchanged by this ADR.
- Rollout fails closed in either order (§1): an older anyrt cannot
  create or use the write grant, and new connectors on it answer
  "update the app". The cost is a second consent screen the first time
  the user asks for a draft or a hold, and a second refresh token.
- Cost accepted: provider-specific host code. The descriptor names the
  checks and the code implements them, so a new Google write is an
  amendment, a rule and possibly a check. Each guarded update or
  delete costs one extra read. Because the list is an allowlist, a new
  send-like endpoint at Google is refused until someone lists it.
- The draft marker and the event-id prefix are Bao's notion of
  ownership. Both are visible to the user (a header in "Show
  original", an id in the event URL) and both can be lost by the user's
  own edits, which is the intended direction.

## Phasing (one topic = one commit)

1. This ADR and its amendments of ADR-011 and ADR-033 (no code).
2. Runtime: `guest_writes` on the descriptor, the checker
   (`oauth_writes.rs`), the broker wiring (refusal, preflights, the
   redirect re-check), the Google table, tests.
3. Connectors: `googleAuth.connect(writes=True)`, Gmail drafts,
   calendar holds, tests, README and skill text.
4. Live hardening on a real account (`repos/_connectors/testing-flow.md`)
   — the owner's.
