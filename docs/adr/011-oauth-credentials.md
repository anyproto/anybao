# ADR-011: OAuth credentials — host-held tokens, `oauth.*` effects, provider descriptors

Status: **Accepted** (2026-07-31; amended in review — open questions
resolved, §4 host binding deferred)
Date: 2026-07-30
Builds on: ADR-002 (effect boundary — credential injection is a
boundary fact, §Design rationale "Secrets"), ADR-006 §3 (device-local
secrets on the derived object), ADR-008 §1–2 (credential refs beyond
the LLM key; the http redirect surface), ADR-009 §6 (lib mode);
`docs/config-secrets.md` (seed/rotate/revoke + the guest read-guard)

## Context

Six token connectors ship (`repos/_connectors`: linear, github,
granola, attio, figma, intercom). The Google family — gmail,
googleCalendar, googleDrive, googleSheets, plus the shared googleAuth
layer — is the one block that never landed, because a static key is
not how Google authenticates: the credential is *minted by a user
consent flow* and *expires hourly*, so acquisition and renewal are
themselves side effects. Today's boundary has neither.

**Source material, not contract.** bobrik-watch shipped a working
version (`internal/anyrt/oauth.go` + `googleAuth@v1.js` on
`feat/bobrik-connectors` in `~/any/any`): a provider-agnostic
`oauthFlow` host fn doing authorization-code + PKCE over a loopback
redirect, returning `{accessToken, refreshToken, …}` **to the guest**,
which then stored the refresh token in guest-readable config, cached
the access token in guest memory, refreshed it with a plain `fetch`,
and set `Authorization: Bearer …` itself.

That shape is unportable here, and the reason is the whole point of
this ADR: **an OAuth token is a secret**, and under ADR-002/006/008
secrets never enter guest memory, guest-readable config, or the trace
— they are named by ref and injected host-side after recording. The
bobrik design violates all three. What survives verbatim is its
*protocol* work (PKCE S256, `state`, loopback redirect, the Google
scope union); what changes is *custody*.

The mechanics needed on the storage side already exist and need no
extension: the `agent_secrets` dataset takes an **open ref set** with
device-local never-synced values, hard-seed rotation, empty-value
revoke, and a guest read-guard (`docs/config-secrets.md`).

Second forcing function: **remote instances.** The desktop case (serve
and human on one machine) is solvable with a loopback listener, but the
target is authorizing an `anyrt` running elsewhere, where no browser
exists next to the process. That is a *transport* problem, not a
protocol problem, and this ADR fixes the seam so the remote transport
is an added implementation, not a redesign.

## Decision

### 1. Custody rule (the invariant everything else serves)

**No OAuth token — access, refresh, or authorization code — is ever
returned to guest code, written to a synced field, or recorded in a
trace.** The host acquires, stores, renews, injects, and revokes.
Guest code names a ref and reads statuses.

Corollary, and the reason this is stated first: the guest **also never
receives the consent URL**. It carries `client_id`/`state`/
`code_challenge` — not secrets, but handing URL delivery to the model
makes the phishing surface model-writable (a cell could relay a
lookalike URL into the chat). The host delivers consent to the human
(§5); the guest learns only that consent is pending.

### 2. Provider descriptors: genericity as data

A host-side table, `runtime/src/oauth_providers.json`, embedded with
`include_str!`. The mechanism matches `config_defaults.json`
(ADR-006 §3) but **not** its destination: defaults merge into
guest-readable config, this table never does (the `MODEL_PRICING`
precedent). Keyed by provider id:

```json
"google": {
  "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
  "token_url":     "https://oauth2.googleapis.com/token",
  "revoke_url":    "https://oauth2.googleapis.com/revoke",
  "auth_params":   {"access_type": "offline", "prompt": "consent",
                    "include_granted_scopes": "true"},
  "default_scopes": ["openid", "email", "…/gmail.readonly", "…"],
  "rotates_refresh_token": false,
  "client_auth": "post_body"
}
```

Adding Microsoft/Notion/Slack later is a table row, not code. The
table is **host-only data** — embedded like the pricing table, never
merged into guest-readable config; `default_scopes` is the consent
default when the guest passes none. Injection metadata and destination
allowlists are deliberately *not* here — they belong to the deferred
host-binding design (§4).

PKCE S256 is unconditional for every provider. No descriptor flag
disables it; a provider that cannot accept a `code_challenge` ignores
it harmlessly.

### 3. Managed credential refs (amends ADR-008 §1)

A ref is now one of two kinds, distinguished by the ref namespace, not
by a new store:

- **static** — `connector.key.<name>`, `llm.key.<provider>`: today's
  behavior, a stored string injected as-is.
- **managed OAuth** — `connector.oauth.<provider>`: not a stored
  string at all but a *handle* to a provider descriptor plus these
  device-local records in `agent_secrets` (open ref set, so zero
  runtime change to store them):
  - `connector.oauth.<provider>.refresh` — the refresh token. The
    crown jewel: durable, offline, equals standing scoped account
    access. Device-local, never synced, never guest-readable.
  - `connector.oauth.<provider>.client_id` /
    `.client_secret` — seeded through the ordinary `.connectors.env`
    path. Bring-your-own client (Google Cloud Console → Credentials →
    OAuth client → **Desktop app**). The "secret" of a desktop client
    is a public-client secret: it is shipped to users and proves
    nothing; PKCE is the actual protection. It is stored as a secret
    anyway (no reason to leak it) but MUST NOT be treated as an
    authentication factor in any decision here.
  - `.granted_scopes` / `.account` — non-secret grant metadata, synced
    `value` (they are useful in the UI and carry no authority).

Access tokens are held **only in host memory** — the managed OAuth
state shared across runs, keyed by ref, with expiry. (Shared, not
per-run: Google caps live tokens per client per account, and a
per-run cache would mint one per conversation/cron run.) They are
short-lived by construction; persisting them buys a few minutes of
warm start against a durable at-rest secret. Not worth it.

### 4. Host binding deferred; guest shape stays `{ref, header, prefix}` (resolves review Q4)

Today a guest payload carries `{ref, header, prefix}` and the broker
injects wherever the guest says — any cell can send any secret to any
host in any header, the filed gap (`broker.rs` `sys_http`, dev-space
task "easy UI to insert initial secrets" § Security). The draft closed
it here as a precondition; review (2026-07-31) resolved it the other
way. The `.connectors.env` seeding path is a deliberate **UX
stopgap**, and per-secret destination metadata belongs to the *proper
secret-entry flow* that will replace it: each secret ships with its
host/scope restrictions (most likely bundled with its connector), the
human double-checks the scope at entry, and the design must also cover
the flow where the agent authors a new connector for the user. That
design lives in the "easy UI to insert initial secrets" task — not
here.

Until it lands: **raw secrets, no host checks.** The six existing
connectors are untouched, and a managed OAuth ref uses the *identical*
guest shape — `{ref: "connector.oauth.google", header:
"Authorization", prefix: "Bearer "}` — the only difference being how
the broker resolves the value (§6).

What custody still guarantees despite the open gap: the **refresh
token — the durable secret — is never injectable.** Managed sub-refs
(`connector.oauth.google.refresh`) are not provider handles, so naming
one in a credential is a typed error, and the OAuth sub-ref values are
drained out of the broker's static secret map entirely (§6). What the
gap exposes for OAuth is only the in-flight access token, bounded by
its ~1h life.

**Amended 2026-08-02 (E8): the redirect half no longer waits for the
binding design.** ureq 2.12.1 (verified 2026-07-31) made it two live
bugs, not a future concern: the client's redirect strip list is by
header NAME (`authorization`, `cookie`, `content-length`), so any
*other* header a guest names (`X-Figma-Token`, `x-goog-api-key`) is
replayed verbatim at whatever host answers 3xx — attacker-chosen or
not — while `authorization` is stripped on **every** redirect
including same-host, silently 401ing a legitimate Bearer follow. The
broker now owns the follow decision for credentialed requests
(`sys_http`):

- No `redirects` in the payload → **manual** (`redirects=0`): the 3xx
  and its `location` come back as data — the ADR-008 §2 shape, no new
  surface.
- An explicit `redirects` count follows **same-origin only** (scheme +
  host + port), the credential re-attached host-side per hop;
  301/302/303 downgrade a non-GET/HEAD hop to a bare GET, 307/308
  re-send the body. A cross-origin 3xx is returned as data, never
  followed, and every hop passes the secrets read-guard.
- Uncredentialed requests keep the client's own policy (default
  follow, limit 5) — unchanged.

Host *binding* — which hosts a ref may be named for at hop zero —
stays deferred to the secret-entry-flow design above (E7).

### 5. `oauth.connect` — consent as an effect that returns no tokens

New effects — broker match arms like every syscall (the `@effect`
decorator of earlier ADR drafts is spec notation, not shipped code;
there is no `redact=` machinery — redaction is structural, §9). `cap`
defaults to the effect name, so `oauth.*` grants work through the
existing prefix match with no capability code:

| effect             | kind   | contract                                                        |
|--------------------|--------|-----------------------------------------------------------------|
| `oauth.connect`    | mutate | `{provider, scopes?, timeout?}` → consent; returns no tokens    |
| `oauth.status`     | read   | `{connected, pending, scopes, account, expiresAt}`; no network  |
| `oauth.disconnect` | mutate | provider revoke + local delete                                  |
| `oauth.refresh`    | mutate | **host-emitted only** (§6); direct guest call → typed `host_only` failure |

`oauth.connect` returns **`{ok, provider, grantedScopes, account}`** —
no token material, ever. Internally it: binds the redirect receiver →
generates `code_verifier`/`code_challenge` (S256) and `state` → builds
the consent URL → hands it to the human over a **transport** (§5.1) →
receives the code, checking `state` by exact match → exchanges the
code at the token endpoint host-side over TLS → writes the refresh
token device-local → caches the access token in memory.

**Blocking (resolves review Q3):** `connect` blocks for
`min(timeout | 120s, 300s)` while the human clicks. On timeout it
fails typed `consent_timeout` — but the receiver keeps running to the
end of its fixed **5-minute window**: slow consent (2FA, an account
chooser) is the *common* case, and killing the listener at 120s turns
it into a dead-end browser error plus a full re-consent for zero
security gain — the window, the single-use receiver, `state`, and
PKCE are the actual bounds. Late completion lands host-side; the
guest observes it by polling `oauth.status` (hence `pending` in its
shape). A concurrent `connect` for the same provider **joins** the
pending flow rather than spawning a second receiver. The receiver is
single-use, torn down on completion, `state` mismatch, or window
expiry. A non-blocking variant (completion as a mailbox/trigger
event) is a dev-space exploration task, not this ADR.

Failure modes are typed and actionable, not strings to grep:
`not_configured` (missing `.client_id`/`.client_secret` — the message
names the `.connectors.env` keys to seed), `consent_denied`,
`consent_timeout`, `state_mismatch`, `token_exchange_failed`,
`no_refresh_token` (the provider returned none — for Google this
means `access_type=offline`/`prompt=consent` were lost, which is a
descriptor bug worth naming loudly rather than failing an hour
later).

#### 5.1 Consent transports (the remote-ready seam)

The host trait is `ConsentTransport`: given a consent URL, deliver it
to the human and yield an authorization code (+ the redirect URI used,
which must match what the exchange sends).

**Local and remote are the same flow — only the listener moves.** A
remote `anyrt` is still operated from an app; the app is simply not on
the serve's machine. So both transports run the identical RFC 8252
native-app pattern (system browser + loopback receiver on the human's
machine), and the only variable is *whose* 127.0.0.1 hosts the
receiver and how the code gets back. There is no second protocol to
design for remote, and no protocol difference to security-review
twice.

Two invariants hold across every transport, and they are what make the
seam safe rather than merely convenient:

- **`code_verifier` and `state` are generated by, and never leave, the
  machine that performs the token exchange** (the serve). A transport
  carries the *code* back, nothing else. Shipping the verifier
  alongside the code would void PKCE on exactly the hop it exists to
  protect — both halves in one message is the same as no PKCE.
- **The serve never accepts tokens from a transport.** A transport
  that did its own exchange would have to send back a refresh token —
  trading a single-use, minutes-lived secret for a durable one. The
  exchange stays host-side, always.

- **A. Loopback (this ADR implements).** Listener on
  `127.0.0.1:<ephemeral>`; redirect URI
  `http://127.0.0.1:<port>/oauth/callback`, which Google special-cases
  for Desktop clients so any port matches (RFC 8252). The URL reaches
  the browser by, in order: the embedder's consent hook in lib mode
  (`ConfigBuilder::consent_hook` — a new ADR-009 §6 surface; any-ui
  opens it, because the serve may be a headless daemon and the *app*
  is what owns a browser session); else the system browser via the
  host (`xdg-open`/`open`, no new crate); else stderr, which is the
  CLI's honest path. Bind
  before issuing the URL — a URL whose receiver isn't listening
  produces a dead-end consent. Serve already runs a `tiny_http`
  loopback control API (port 7010); the callback receiver is its
  sibling, but stays a **separate ephemeral listener** — the control
  port is long-lived and shouldn't accept OAuth callbacks outside a
  flow window.
- **B. Delegated (designed here, not built).** For a remote `anyrt`:
  the human isn't at the serve machine, so the *receiver* moves to the
  client — transport A's listener, relocated. The host emits a request
  over the message channel the "easy UI to insert initial secrets"
  task specifies — the chat message's `agent` field, the one channel
  every client sees (desktop and mobile alike), rather than a
  desktop-only command channel. Three hops, and the shape is forced by
  the invariants above:
  1. serve → client: `{kind: "oauth", provider, scopes}`.
  2. client → serve: the `redirect_uri` it has **already bound** on
     its own loopback. The port belongs to the client's machine, and
     the exchange must present the same URI the authorize request
     carried — hence a handshake rather than one message. A fixed
     client port (with an ephemeral fallback) collapses this hop, at
     the cost of a port the client must be able to claim.
  3. serve → client: the consent URL (challenge + `state` built
     serve-side); client opens the system browser, captures the
     redirect, returns **only `{code, state}`**.

  The serve exchanges the code with the verifier that never left it.
  **Why the code and not tokens:** an intercepted authorization code
  is single-use and dies in minutes; an intercepted refresh token is
  durable account access. The write path needs exactly the trust level
  secret insertion needs — no more — and the exposure window is orders
  of magnitude smaller.
  Prerequisite (upstream, not this ADR): the `agent` field on chat
  messages does not exist yet. Specify it verb-extensible —
  `agent.request {kind: "secret" | "oauth", …}` — so this is a second
  verb on shipped plumbing rather than new plumbing.
- **Device authorization grant (RFC 8628) is rejected as the remote
  backbone**, and the reason is recorded so nobody re-proposes it:
  Google restricts the device flow to a small scope list (profile,
  `drive.file`, YouTube-family) — gmail and calendar are not on it —
  and the `oob` copy-paste redirect was removed in 2022. It stays
  available as a per-descriptor option for providers that allow real
  scopes on it.
- **C. CLI (trivial, follows from A).** `anyrt oauth connect
  <provider> [--remote <control-addr>]` runs transport A locally and
  ships the resulting code to a remote serve's control API — the
  no-client fallback for headless boxes.

**Unattended consent does not exist, and no transport will produce
it.** A human must approve once, in a browser, at the provider —
that is what the grant *is*. Everything after that first approval is
unattended (§6 refreshes forever, until revoke / the Testing-status
7-day cap / ~6-month idle), which is what makes a remote agent
practical. The genuine "server OAuth" — a service account with a
signed JWT and no user — is a different product: it reaches only
resources the service account itself owns, or a Workspace domain via
admin-granted domain-wide delegation. It cannot reach a personal
Gmail or Drive, so it is not a shortcut around consent for this use
case. Out of scope; revisit only if a Workspace-tenant deployment
becomes a target.

### 6. Renewal happens inside the boundary, at injection time

When `sys_http` resolves a managed ref: if the cached access token
outlives a safety margin (120s), inject it; otherwise perform the
`refresh_token` exchange host-side, then inject. Rules:

- **Single-flight per ref.** Concurrent cells (and `batch`) must not
  stampede the token endpoint — one refresh, others await it. Google
  caps live tokens per client per account (~50) and a stampede burns
  that budget.
- **Rotation-safe persist.** Providers with
  `rotates_refresh_token: true` invalidate the old token on use;
  persist the new one **before** the new access token is used, or a
  crash between the two bricks the connection.
- **Recorded as its own effect record** — `oauth.refresh`, sequenced
  before the http record that triggered it, stamped
  `meta.hosted: true` + `meta.credential: {ref}` (the batch
  precedent, ADR-002 §Resolved Q3: the host may emit additional
  records within one crossing). Host-emitted records skip the
  capability gate — the guest asked for the http cap; the refresh is
  the host's implementation detail, and a guest lacking
  `oauth.refresh` must not be able to break credentialed http. Output
  is `{ok, expiresAt, scope, rotated}` — no token material. If it
  isn't in the trace it didn't happen; this is the one
  nondeterministic step that would otherwise hide inside an http call.
- **Replay never needs a secret.** Both records replay from the
  trace; no exchange, no store read. One mechanism makes this true:
  host-emitted records precede the guest record that triggered them,
  so the strict replay cursor drains `meta.hosted` records ahead of
  the expected guest record instead of diverging on them. (The
  pre-existing `batch` records have the same trace shape and today's
  cursor does *not* drain them — a filed bug, fixed separately.)
- **The OAuth sub-ref values live outside the broker's static secret
  map.** At bootstrap every `connector.oauth.*` entry is drained into
  the managed state, so per-run secret snapshots never contain a
  refresh token and the static injection path structurally cannot
  leak one (§4).
- **`invalid_grant` is a lifecycle event, not a bug.** Refresh tokens
  die from user revocation, password change (gmail scopes), ~6 months
  idle, per-client limits, and — the one that bites during
  development — **7 days when the Google consent screen is in Testing
  status**. Publishing the app (even unverified) lifts that. The
  broker maps it to a typed `oauth_reconsent_required` failure whose
  message says to re-run connect, and drops the dead refresh token.

### 7. Guest surface: `googleAuth@v1` and the family

In `repos/_connectors`, matching the existing connector conventions
(ADR-010 docstrings, `@span` per method, `{ok, …}` / actionable
`{ok: false, error}`):

- `googleAuth@v1` — `connect(scopes?, timeout?)`, `status()`,
  `disconnect()`, thin wrappers over the effects (the raw `effect()`
  guest global — no kernel change), plus the shared
  `_CRED = {"ref": "connector.oauth.google", "header":
  "Authorization", "prefix": "Bearer "}` the family imports (§4:
  today's full shape). It holds **no** token logic — the bobrik
  module's `refresh()`, `token()`, `authedFetch()` all evaporate into
  §6. Its docstring notes `connect` is for user-facing turns only (it
  blocks up to 120s — never call it from cron programs).
- gmail / googleCalendar / googleDrive / googleSheets — ordinary
  read-only REST connectors passing `credential=_CRED`, structurally
  identical to github@v1. Ported from the bobrik branch for endpoint
  shapes and field trimming (C1: trim, don't rename), not for auth.
- **Not-connected error contract.** The six static connectors keep
  string-matching `"no secret for credential ref"` — that message
  stays byte-identical. The Google family prefix-matches the typed
  failures from §5/§6 on the flattened `EffectError` string
  (`"not_connected: …"`, `"oauth_reconsent_required: …"`,
  `"not_configured: …"`, `"consent_timeout: …"`), each mapping to a
  connector message that says what to do — the same standard as
  github's not-connected text, pointing at `googleAuth.connect()`
  instead of the .env import sentence.

**One credential per provider account, not per connector.** A single
`connector.oauth.google` consent covers the scope union of every
installed Google connector, with `include_granted_scopes=true` for
incremental re-consent when a new one needs more. Per-connector Google
credentials would multiply consents and refresh-token lifecycle for
zero isolation gain — they would all be the same Google account.

Scope tiering matters operationally: the default union stays in
Google's *sensitive* tier (gmail.readonly, calendar.readonly,
drive.metadata.readonly, spreadsheets.readonly) and out of the
*restricted* tier (full `drive.readonly`), which additionally requires
a CASA security assessment. A BYO client in Testing mode is unaffected
by verification, but the tiering decides what a shared published
client could ever ask for.

### 8. Revoke, rotate, and the store

- `oauth.disconnect(provider)` POSTs the provider's revoke endpoint
  **and** deletes the device-local refresh token. Provider-side revoke
  is the part that actually ends access; local delete alone leaves a
  live grant on the account.
- The existing hard-seed revoke path works unchanged as the blunt
  instrument: an empty value for `connector.oauth.google.refresh` in
  `.connectors.env` deletes the stored token at next serve start
  (`docs/config-secrets.md`). It does **not** revoke provider-side —
  document that difference where it's read.
- A refresh token may also be seeded by hand through the same file
  (migration from another install, or a CI-injected demo build). The
  open ref set makes this free.

### 9. Trace, redaction, and the read-guard

- `oauth.connect` records `{provider, scopesRequested}` in, and
  `{ok, grantedScopes, account}` out. **Redaction is structural, and
  that is the whole mechanism**: there is no `redact=` machinery in
  the runtime — the consent URL, `state`, `code_verifier`,
  authorization code, and every token are simply never placed in any
  effect payload, output, or record; they live only in host memory
  and the flow thread. (Same principle that keeps static secrets out
  of traces today: the credential value is resolved after the
  canonical input is recorded.)
- The `agent_secrets` read-guard already refuses guest http that
  targets the secrets dataset or object; the new refs live there, so
  they inherit it with no change.
- `config.get` refuses secret keys. Static refs are refused by
  presence in the secret map (unseeded falls through to "no config
  value" — nothing to leak); the `connector.oauth.*` namespace is
  refused **wholesale**, because its values live in the managed state,
  never in that map (§6's drain), so presence could not cover them.

### 10. Phasing

One topic = one commit; the ADR is accepted before any code lands.

1. This amendment + the dev-space task updates (no code).
2. §2/§3/§6 — descriptors, managed refs, refresh-at-injection with
   the host-emitted `oauth.refresh` record and its replay drain.
   Independently testable with a hand-seeded refresh token via
   `.connectors.env` (§8) — no consent flow yet.
3. §5 — `oauth.connect`/`status`/`disconnect` + transport A (the
   loopback receiver, consent delivery, the lib consent hook).
4. §7 — `googleAuth@v1` + the four Google connectors, hardened
   through `repos/_connectors/testing-flow.md` (the per-connector
   question checklist on the :7009 rig).
5. Transport B when remote instances become real, gated on the
   upstream `agent` message field.

## Consequences

- OAuth-class providers become reachable with tokens held to a
  *higher* standard than today's static keys: acquisition, renewal,
  and revocation are all recorded effects, and no token is expressible
  in guest code.
- The credential surface stays uniform: guest code sends the same
  `{ref, header, prefix}` shape for every ref and cannot tell a
  static key from a managed OAuth grant — the difference is entirely
  in host-side resolution. Every future auth scheme (mTLS, signed
  requests, AWS SigV4) plugs in at the same seam.
- The filed exfiltration gap stays open — deliberately, per §4 — and
  its closure moves to the secret-entry-flow design. OAuth narrows
  its blast radius anyway: the refresh token is structurally outside
  the injection path; only ~1h access tokens transit it.
- Remote authorization stops being a redesign and becomes a transport
  implementation.
- Cost accepted: a new host-side listener with a real security
  contract (state, PKCE, single-use, timeout) to maintain; refresh
  latency lands inside the first http call after expiry (mitigated by
  the 120s margin); provider descriptors are a table that will drift
  from providers' actual behavior and needs live-verification per
  provider; `connect` blocks a run thread for up to 120s.
- Deliberately not solved here: a shared published anybao OAuth client
  (needs Google verification, and for restricted scopes a CASA
  assessment) — BYO Desktop client is the shipping path; account-scoped
  rather than device-local secrets (open exploration in the dev-space
  task, gated on at-rest guarantees); token-level capability scoping
  (which cell may use which ref).

## Resolved questions (review, 2026-07-31)

1. **Ref naming** — dotted sub-refs
   (`connector.oauth.<provider>.refresh` / `.client_id` /
   `.client_secret` / `.granted_scopes` / `.account`), as §3
   describes. Plain strings in the store, individually seedable; no
   JSON blob.
2. **`.client_secret` provenance** — BYO Desktop client is the
   shipping path; a published anybao client stays out of scope until
   it's a roadmap item.
3. **Blocking** — blocking with a 120s default plus `oauth.status`
   polling (§5). A non-blocking mailbox/trigger completion is a
   dev-space exploration task.
4. **Host binding / `allowed_hosts`** — deferred wholesale to the
   secret-entry-flow design (§4); raw secrets, no host checks, until
   it lands.
5. **Multi-account** — a later ref-suffix convention
   (`connector.oauth.google#work`); one account per install for now.
