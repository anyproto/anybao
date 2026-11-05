# Fresh ChatGPT OAuth on current Bao

Runtime contract, 2026-11-03. This is the Step 7 port onto
`fb1de0f0732e296693116b1b0e80f64d7485ad1d`. Desktop integration follows in
Step 8. ADR-030 remains the independent judge-tier decision.

## 1. Two independent ways to use a subscription

The existing `anyai` backend runs the desktop's local HTTP proxy, which
invokes Codex or Claude Code as subprocesses. It remains `openai-compat`,
uses `llm.key.anyai`, sends plain JSON with the existing 590-second timeout,
and supports the existing CLI tool relay and web search. Its discovery,
login, model catalogue, port repair and auto-connect behavior are unchanged.

Fresh OAuth is a separate, explicit choice:

```json
{
  "provider": "openai-responses",
  "backend": "openai-codex",
  "model": "<explicit model id>",
  "base_url": "https://chatgpt.com/backend-api/codex",
  "api_key_ref": "connector.oauth.openai-codex"
}
```

This route obtains its own grant through the native host. It never discovers,
imports, recovers or falls back to Codex, Hermes, another application's tokens,
an API key or a CLI session. Config contains a handle, never a token. A synced
OAuth selection on a device without the native grant is unavailable there.

## 2. Native ownership and custody

The embedder constructs one `Arc<chatgpt::ChatGptAuth>` per authenticated Any
session, and supplies it through `ConfigBuilder::chatgpt_auth`. The service
is shared by native commands and runtime brokers. There is no runtime-created
filesystem token store or CLI credential adapter.

`GrantSlot::new(application_namespace_and_channel, stable_profile_id,
any_account_id, device_id)` takes native authenticated identity only. The
hashed storage key binds all those fields, the custody namespace and provider.
Renderer-supplied account or filesystem identifiers are not authoritative.

`LocalGrantStore` is the native custody interface. `read` and `replace` must
be bounded and noninteractive. The implementation must encrypt grants in
device-local custody, never synchronized account data. `replace` is atomic
and durable, leaving a complete old or new envelope even on error. The
opaque `GrantEnvelope::encode/decode` methods exist for that implementation;
they must never be renderer commands, logs, exports, traces or diagnostics.
The desktop encrypted vault implementation is part of Step 8. The optional
`prepare_access`/`PreparedGrantAccess::commit` transition is exclusively for an
explicit native user action: preparation may request OS authorization for at
most 120 seconds, observes cancellation, and cannot publish a record. The core
retains the canonical transaction and commits only under its session and exact
operation cancellation gates. Ordinary read/replace never use this transition.

Ordinary secret seeding, connector OAuth, `AgentHandle::set_secret`, synced
secret reads/writes and guest config access cannot serve the reserved exact
handle or its dot descendants. No synchronized credential row is needed for
native OAuth readiness or inference.

## 3. Cross-process transactions

`GrantLockDirectory` must be a stable private application directory shared by
all processes using the slot. Its lock files contain no credentials. Do not
delete or replace them while installed. An in-process gate and OS file lock
serialize the same canonical slot across threads and processes. All envelope
reads and replacements relevant to login, refresh, disconnect and inference
completion run under that transaction.

An access/refresh pair is one versioned envelope with one grant identity.
Before refresh sends anything, the host durably writes `refresh_inflight`.
An ambiguous send, invalid success payload, or interrupted process requires
fresh login; another process never retries a potentially consumed refresh
token. A successful rotated pair is persisted before inference can use it.
If the endpoint omits the refresh field, a valid old refresh token is retained;
an explicitly invalid replacement is rejected. Issuer, subject and account
identity must remain consistent across refresh.

The shared transaction also reserves durable monotonic boot generations for
readiness. Generation counters and lock files are separate from token data.

## 4. Fencing and lifecycle

Every login belongs to a session generation, owner instance and durable slot
epoch. Cancellation, replacement login, disconnect and Any logout fence old
workers. Late completions cannot install into another session or account.
Disconnect writes a tombstone; a failed deletion suspends local use.

The embedder must call `ChatGptAuth::shutdown` before discarding a service on
Any logout/account switch. It cancels pending login work, waits for an
already-sent refresh to settle in its original slot, and cleans up only its
own pending login. Stopping/restarting Bao alone need not end the native Any
session or disconnect its grant.

Each logical `llm.chat` span owns one inference binding, total deadline and
authentication-recovery budget. A known, nonstreaming token rejection permits
one refresh/adoption retry. Quota, policy, unknown authentication failures and
stream errors do not refresh. Completion is checked under the slot lock before
any response is released; account replacement or logout invalidates it.

### Explicit custody recovery (2026-11-04)

`authorize_store` is separate from login, refresh and status. macOS uses a
validated, isolated helper with an interactive policy and its own bounded
lifetime. The UI process never changes global Keychain interaction policy.
The helper unlocks the default Keychain if needed, reads the old wrapping key
with user consent, and creates a fresh immutable item owned by the current
executable. Before publishing re-encrypted ciphertext, the parent must read the
new item through a separate noninteractive helper. Thus one-time **Allow** can
be sufficient; a successful interactive read alone never establishes recovery.
First-key creation follows the same explicit path. Failed/cancelled preparation
may leave an unreferenced immutable OS item but cannot replace the current file.
Wrapping keys are never cached outside OS custody. This is source/test evidence,
not a claim that a real OS dialog or application signature was accepted.

Errors distinguish required interaction, denied access, missing key, damaged
current-format ciphertext, unsupported schema and unavailable service. Missing
keys cannot decrypt old ciphertext. `prepare_recovery` captures a host-only
exact-record fingerprint under the slot lock. A native window/session-owned
confirmation ticket is required for `reset_store`; confirmation rechecks the
exact record and unrecoverability under that same cross-process transaction.
Readable grants, changed bytes, unknown outer formats, and decodable unknown
inner schemas cannot be reset. A recognized outer v1 record with a demonstrably
missing key can be explicitly reset even though its encrypted inner schema is
unknowable. No generic invalid-record error authorizes destruction.

Reset atomically writes a nonsecret marker to only the canonical local ChatGPT
slot. It never deletes OS keys or other files. An exact marker can settle an
ambiguous final directory sync on retry; a newer record cannot. Status treats
the marker as empty and later sign-in uses a fresh key identity. Core session
fences and unique pending-login/grant IDs prevent old workers or responses from
reviving a connection even when the empty envelope's epoch restarts. Old reset
tickets cannot discard a new grant. A failed disconnect remains suspended for
inference but can diagnose custody; explicit authorization finishes its original
disconnect before clearing suspension.

Native ownership captures the window incarnation before authorization/queueing.
Window closure, explicit cancellation and session retirement revoke exact
operations before a later commit. Cancellation and final publication share a
gate. Retirement calls `revoke_session` before joining settlement via `shutdown`;
already-sent refreshes still settle only in their original slot. Background
operations remain noninteractive and bounded while another process holds a
recovery transaction. Linux/Windows retain silent custody: the desktop directs
users to unlock their system store and check again; scoped reset is portable.

## 5. Login and host integration API

The fixed public client supports fresh browser PKCE and device-code login.
Authorization/token URLs are fixed at `auth.openai.com`. Browser callback is
`http://localhost:1455/auth/callback`, bound on loopback; state, method, Host,
path, duplicate query parameters and bounded lifetime are checked. Device
polling respects expiry, rate limits and cancellation. No caller supplies a
token URL, callback destination or browser-open URL.

The native host exposes appropriately authorized wrappers for:

- `get_connection()` and `subscribe()` — local snapshots, no inference or
  token refresh; revision and session generation order events.
- `start_login(LoginFlow::{Browser, DeviceCode})` — transient initiating-window
  presentation plus login identity/expiry.
- `open_login(login_id)` — opens only the current stored presentation through
  the trusted `LoginBrowser` implementation.
- `cancel_login`, `cancel_login_if_owned`, `disconnect`, and `shutdown`.
- `authorize_store`, `prepare_recovery`, `reset_store`, and `revoke_session` —
  native custody/lifecycle only; no caller supplies a key, record or slot path.

Local presentation may include the account label, plan label, user code or
authorization URL. Those fields never go to the account event bus. Errors are
fixed `AuthErrorCode` values, never raw HTTP bodies, platform errors or tokens.
The guest's ordinary `oauth.*` effects cannot operate this provider.

## 6. Protected inference and recording

The only credential-bearing destination is an HTTPS POST to
`https://chatgpt.com/backend-api/codex/responses` (explicit default `:443` is
also accepted by the host). Raw equality precedes URL parsing. No redirect,
proxy configuration, alternate path, query, fragment, userinfo, extra header
or configurable bearer destination is admitted.

The guest supplies the opaque credential ref. The host expands Blob input,
checks the bounded request schema, and injects access/account/residency headers
only inside the transport. Requests require `store:false`, streaming, stateless
input and encrypted-reasoning inclusion. Native function tools and inline
PNG/JPEG/GIF/WebP images are supported; text files retain the inline size cap.
Only `reasoning.effort` and `text.verbosity` are supported request options.
Native hosted web search, PDF input, sampling options and text-tool profiles
are not advertised by this provider.

Provider errors become fixed safe envelopes. Success events are parsed and
inspected, including assembled text/refusal/function argument fields and their
decoded JSON, before trace/blob writers see them. Reflected credential
material is rejected. Partial output-limit function arguments are removed
before recording. Native auth/inference records contain safe outcomes and
attempt numbers; strict replay drains hosted records without touching custody
or making requests. Mocked calls preserve subscription billing provenance.

## 7. Responses lifecycle and accounting

The guest folds complete SSE frames with unique ordered items, stable response
and item identities, monotonic sequence numbers when supplied, field/part
completion checks and exactly one explicit terminal event. Duplicate JSON keys,
non-finite arguments, unfinished calls, conflicting deltas and contradictory
terminal output fail closed with fixed diagnostic codes.

For a successful `response.completed`, missing, null or empty terminal
`response.output` uses the ordered completed `response.output_item.done` list.
A nonempty terminal list must match exactly. This is the previously live-tested
backend omission behavior, now covered with synthetic fixtures; the port itself
does not perform live validation. An incomplete/failed response is never
promoted to executable tools. Explicit output-token limits retain completed
text, usage and `stop:length`, while discarding partial calls. Text cannot be
lifted to fenced/XML tools after a length stop.

Live history preserves message order/phase, refusal text, function `call_id`
and opaque reasoning. Lookup item IDs and response IDs are not replay handles.
Opaque reasoning is resent only for the same provider, endpoint and model;
durable conversation turns keep display text and statistics, not that state.

Usage counts input, cache reads and output without double-counting cached
input. Missing usage stays marked unknown. Host/guest accounting labels
`chatgpt-subscription`; subscription and mixed-source monetary totals are null,
never inferred from an API model price or reported as free.

## 8. Explicit configuration and readiness

`config@v1.apply_models(spaceConfig, tiers, search=None)` submits one complete
fresh selection: `llm.configuration.target`, all three codegen/classify/vision
rows, intended search rows and a cancellation reset in **one modify batch**.
The SDK applies the batch as one synced DAG change and one database transaction.
This is the selection's commit point; there are no later per-tier writes.
Readback verifies the target and every intended value, including after an
ambiguous response or per-op rejection. Rejections can leave holes, so a 200
response alone never establishes a matched configuration.

Every Apply obtains a new host UUID and sends its batch once. Neither the guest
nor the desktop may automatically resend an ambiguous old batch after another
selection or cancellation. `retry_models(spaceConfig, apply_id)` is read-only
delivery verification for that expected ID. It reports superseded, abandoned
or incomplete honestly; it never repairs or reinstalls the old intent. An
incomplete/rejected selection requires a new explicit Apply with a fresh ID.
The guest Any write helper has no retry loop, and the runtime transport does
not retry a nonempty POST after sending its body. Step 8 must preserve this
invariant in its own HTTP helper and UI actions.

`abandon_models(spaceConfig, apply_id)` adds the SHA-256 of its observed apply ID
to `llm.configuration.abandoned` using `$addToSet`. It never clears the target
or changes model rows. A late cancellation for A therefore cannot erase B's
target or cancellation. The marker array contains at most 1,024 unique lowercase
64-character hashes; malformed or oversized data cannot establish readiness.
Each fresh atomic Apply resets that array to `[]`. Additions commute, and an
older addition cannot undo a newer reset. A canceled current target provides
no matched-intent claim; this is cancellation of the claim, not rollback of
already committed model settings. A subsequent fresh selection works normally.
Ordinary `set`/`set_model`, CLI port repairs and the independent judge remain
raw-row operations with their existing behavior.

The manifest is a JSON string `{schemaVersion:1, applyId, tiers, search?}`.
Optional `search` names only `websearch` and/or `deepresearch`; it is included
in identity and matching when supplied. Existing v1 three-tier identities are
unchanged. The optional TypeSafe judge tier is independent and preserved.
Ordinary API and `anyai` rows retain current traits and backend semantics.

When the UI switches a CLI choice to OAuth, it must follow current main's
existing search contract: reset only a CLI-owned search row to the existing
default Gemini row, preserve custom search rows, and include an intended search
change in the target. OAuth does not promise web search. Explicit OAuth choice
must never be replaced by first-run CLI auto-connect or proxy port repair.

Rust and the guest use the shared counted UTF-8/binary64 canonical identity
fixtures. Readiness comes from one raw bounded collection query; duplicate,
aliased or mismatched row identities cannot establish readiness. It matches
the uncanceled target against every intended row, without default filling or
inferring intent from a partially applied set. Web search accepts Gemini and
the existing `openai-compat`/`anyai` proxy; deep research remains Gemini-only,
matching the actual guest programs.

`bao.status.data.chatgpt` carries the safe `chatgpt-local-v1` extension alongside
existing responder identity, role and state. `publisherScope` binds native
custody scope; positive decimal-string `bootGeneration` and `sequence` preserve
full u64 ordering. The runtime reserves before publishing, increments on every
beat, republishes on configuration/provider changes, and retains ordering on
shutdown. Projection runs on one background worker with a capacity-one result
channel and at least a one-second pause between requests. Its production HTTP
client has a separate 500 ms request/connect budget; ordinary runtime requests
retain their existing timeouts. Results expire two seconds after their read
started, so a delayed success cannot renew readiness. The publisher only polls
the channel: blocked queries or custody cannot block ordinary beats or stop.
Stop/drop fences late delivery and prevents further reads; the worker retains
only its original source/session. Failed or expired projection reports
unknown/unavailable. The existing active-responder contract still decides where
Bao answers. Native connection alone never establishes model readiness.

## 9. Validation and deployment boundary

Tests use synthetic JWT-shaped strings, fake custody, independent test processes,
temporary lock directories and scratch loopback services. No tests should read
personal profiles, CLI credentials, real keychains or wallets, launch the app,
or contact live authentication/inference endpoints.

Run the pinned Python replay suite and runtime default/shell tests, clippy and
format gates in a task-owned environment. Shared protocol fixtures are in
`docs/fixtures/chatgpt-*.json` and
`tests/fixtures/responses-codex-lifecycle.json`.

A native rebuild alone does not load changed guest programs: desktop Bao uses
configured overlay spaces. Step 9 must deploy these guest sources to an isolated
scratch overlay and verify source readback, or use an explicit local override.
Do not publish to production/nightly/shared overlays as part of this port.
The user's interactive OAuth and inference checks happen after the native UI
integration and review gates, on a profile they choose.
