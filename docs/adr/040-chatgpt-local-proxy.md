# ADR-040: ChatGPT subscription access through the desktop local AI proxy

Status: **Proposed — explicit owner acceptance required before implementation**
Date: 2026-11-09
Builds on: ADR-002 (HTTP/replay), ADR-005 (LLM history/retries), ADR-021
(credential references/host binding), ADR-023 (local traces and run summaries).
Amends if accepted: ADR-005's optional usage metadata and statistics (§8);
ADR-021 §8's host-rebinding rule for this desktop-owned credential (§3).
Also requires an amendment to any-ui's
`docs/specs/2026-10-13-local-ai-proxy.md` for direct dispatch and route authentication.
Does not amend connector OAuth (ADR-011), inference retries or malformed-call recovery.
Decision input: [Bao PR 142](https://github.com/anyproto/anybao/pull/142#issuecomment-6738219710)
and [UI PR 3002](https://github.com/anyproto/any-ui/pull/3002#issuecomment-6738227110).

## 1. Decision and scope

Fresh ChatGPT sign-in must work without Codex/Claude Code installed and without
importing their tokens. Following Tolya's review, **the native desktop `ai_proxy`
owns subscription OAuth, custody and upstream inference**. Bao uses its existing
`openai-compat` provider, `anyai` backend and recorded `http.post` effects.
OpenAI tokens never enter anyrt APIs, the guest, synced secrets or model rows.

Rebuild from current main, reusing suitable auth/custody/UI code and fixtures from
the unreleased branches. Existing CLI providers stay. The only Bao changes are
small, generic accounting changes; no ChatGPT runtime service, broker interception,
reserved OAuth reference, readiness API or model-selection bridge is introduced.
This proposal does not redesign provider navigation, election, model transactions,
CLI accounting/port repair, or desktop-wide diagnostics. Review clearance is not
acceptance; the v1 limitations below are part of what the owner accepts.

Source baseline: Bao `fb1de0f0732e296693116b1b0e80f64d7485ad1d`, UI
`76d87b83b3be96daee20539e5b01ccb7c694fbe4`, pinned any-ai
`6a415733c6b643c8aa5bbbe6251daddc09155492`. Principal sources are UI
`src-tauri/src/ai_proxy/{mod,wire,relay,config}.rs`, `auth_control.rs`,
`local_account.rs`, `instance.rs`, `src/components/model/useLocalAi.ts`,
`src/lib/api/credentials.ts`; Bao `llm@v1/program.py`, `runtime/src/{broker,view}.rs`
and [credential documentation](../config-secrets.md). Prior reference only: Bao
`203bde7ac6bbd5245085d2d04e06dde9201f9db7`, UI `62bd4dc2a5d6a6516e54dd78e8600c4feae2ac8c`.
The official [Responses event reference](https://developers.openai.com/api/reference/resources/responses/streaming-events)
supports general event terminology, not private subscription compatibility or entitlement.
No live profile, credential, app or provider request was inspected for this design.

## 2. Extend the proxy at its dispatch boundary

```
Settings → native connection controller → local custody / fixed OAuth endpoints
Bao → recorded http.post → loopback ai_proxy → direct ChatGPT Responses adapter
                                           ↘ existing any-ai Engine / CLI relay
```

Today's `Engine::run` and `open_session` describe CLI generations and parked tool
sessions. `wire::plan` already converts history for that contract. Add a bounded
model/route parse **before** that conversion: CLI models keep the existing
`wire::plan → Engine → Relay` path; `chatgpt/<model>` uses a direct typed-history
translator and native HTTP transport. Do not represent ChatGPT as an any-ai harness.
Any-ai's rule against credential ownership remains intact. No grant, slot, lock or
custody type is added to or imported from anyrt.

Reuse the listener, OpenAI response envelopes and bounded request admission. The
proxy never executes tools: complete calls return to Bao, which runs its existing
validation/recovery/execution loop and sends results in the next request. Direct
requests carry full history; they do not use parked CLI sessions or a hidden cache.
Retain the shared 16-request cap and `429 busy` overload behavior. Track cancellation
for direct requests by the current native session so logout does not cancel unrelated
CLI requests. Cross-provider queue fairness is unchanged and outside this feature.

## 3. Stable local credential and ordinary configuration writes

The upstream OpenAI grant and the downstream local bearer are separate credentials.
Today's proxy starts before Any login and its `llm.key.anyai` bearer is install-global.
It must not authorize the ChatGPT branch. Native code derives the current Any account
and device from the managed server: bounded coherent reads of `/v1/auth` (`accountId`,
`authorized`) and `/v1/devices` (`self`), as in the prior native identity helper.
Renderer arguments and HTTP payloads cannot choose that identity or a custody slot.
Direct OAuth is unavailable in attach mode, whose server lifecycle the shell does
not own; no new runtime-start/readiness gate is needed.

Persist a random 256-bit local bearer per `(application/channel, profile, Any account,
device)`, in owner-only native storage with atomic replacement, separately from the
OpenAI grant. It follows the existing proxy's local-token storage policy and is
stable through ordinary restart, same-account logout/login, reconnect and ChatGPT
disconnect. A short in-memory session/connection generation controls current admission
and in-flight work (§4); the bearer is not a session-generation protocol.

Use `llm.key.anyai.chatgpt.<installation-id>` for its synced copy. The installation
ID is a random lowercase UUID persisted in this feature's native directory, separate
from `ai-proxy.json`; replacing the CLI proxy's config must not remint it. Different
Any accounts have separate secrets stores and different native bearer values. Losing
this feature's native identity requires an explicit local reconnect/resave, not a
scan or automatic adoption of old rows. No retired-token registry is needed.

Native commands return the local bearer/ref, current base URL and host descriptor
only to a registered workspace webview at the bundled origin (or exact configured
dev origin in debug), checking its current session and window incarnation. OAuth
tokens never cross IPC. **The writer is the webview:** use `setBoundSecret` for value,
label ("ChatGPT on <device>") and exact `127.0.0.1:<port>` hosts, then generic tier
writes. Write the credential first, so installing tiers does not create a missing-key
request. Do not reuse the CLI helper that also overwrites search configuration.

Capture original account/space/anchors/ref; use ordinary JS stale-work checks before
dispatching a write or retry and before publishing its result. Native code cannot
retract a JS request already sent. Accepted/ambiguous writes may commit later;
there is no atomic multi-row selection or rollback promise. Readback is limited to
non-secret status/descriptor/config fields: secret values are write-only in the UI.
Show save failures and allow explicit resave using the known native bearer; never
read stored secret bytes or add a native config-write service/readiness probe.

Retain the proxy's stable port and known previous ports. On a move, this feature's
UI follow rewrites only its exact ref and owned old base URL, using `setBoundSecret`
to update both value and host:port binding. This is the narrow ADR-021 §8 amendment:
the desktop may rebind **its own** open ref when its local proxy moves. It does not
relax guest host binding or fix the existing CLI follow path. Same-port restart needs
no webview repair: the persisted native bearer still matches the stored value.

Tier rows and local bearer values sync within the Any account, just like existing
local-proxy configuration. OAuth tokens, custody and account labels do not sync.
A synced bearer only grants access on its issuing installation while its Any account
is active and its ChatGPT connection is usable. Saving these loopback tiers requires
this computer to be Bao's active responder; reuse responder information and the
existing **Use this device** action. If the responder is elsewhere/unknown, connect
may complete but save waits for the existing responder flow. A later responder move
still requires local connection/selection there; this is the existing loopback
provider limitation, not a new election or sync mechanism.

Keep loopback-only binding, exact Host/port validation, bounded bodies, constant-time
bearer comparison and no cross-origin CORS access. No URL/cookie token or forwarded
local authorization header. Route authentication must occur before any refresh or
inference. On `chatgpt/*`, every unusable local bearer returns **403**, never 401;
otherwise Bao would mark the synced ref rejected and ask the human to paste a key.
Startup identity pending returns 503; a valid local bearer with no usable upstream
grant returns 409 reconnect-required. CLI completion authentication stays unchanged.
`/v1/models` selects the CLI or ChatGPT catalogue by valid bearer, never exposes
another connection, and returns 403 for an unknown bearer without running discovery.
This intentionally amends the UI proxy spec's blanket "wrong token is 401" rule.

## 4. Native lifecycle and rotating-refresh safety

The supported host is one managed desktop process per profile/channel, using the
existing Tauri single-instance guard and managed-server lifecycle. A duplicate launch
focuses the running app; a process without coherent managed identity cannot use
ChatGPT. Arbitrary aliases sharing custody and concurrent external custody writers
are unsupported. No new owner lease, takeover protocol or cross-process epoch/lock
framework is introduced. Use an in-process slot mutex to serialize custody changes.

Store the OpenAI grant in an authenticated encrypted envelope with an OS-protected
wrapping key, private paths, bounded parsing and durable atomic replacement. No
plaintext fallback, CLI credential discovery or token import. Distinguish locked/
denied store, missing key, corrupt record and unsupported schema. Ordinary status
reads report cached state; they do not initiate inference, refresh, consent or an
interactive keychain prompt. Explicit store authorization/reset remains native and
scoped to the current slot, with confirmation before destructive reset.

Refresh when access expiry is within 120 seconds, single-flight under the slot mutex.
Re-read the current pair, durably save `refresh_inflight`, then exchange. Persist the
replacement pair before dispatching with its access token. A crash leaving that marker,
ambiguous exchange, malformed success or failed replacement requires reconnect:
**never retry an ambiguously consumed rotating refresh token**. Only a transport-proven
failure before sending can restore the old pair. Use fresh unpooled token exchanges,
no automatic transport replay, a 30-second exchange deadline and bounded response.
Validate token shape, expiry and consistent issuer/subject/account routing claims;
decoded JWT claims are not signature proof or authority for the Any account.

Keep access-token revision separate from connection generation. A successful refresh
does not invalidate unrelated inference already in flight. Grant replacement,
disconnect or Any-session retirement changes the live generation immediately.
A late auth error may affect only the exact access revision/grant it used. A bounded
5-second slot wait may return pre-dispatch `503 auth_busy`; **completed inference
never waits for this mutex or a keychain call**.

Hook the existing `auth_login*`, `auth_logout`, account removal and managed-server
shutdown transitions; idempotent confirmation of the same live identity is not a
new session. Logout closes admission and cancels its flows/inference synchronously,
then detaches settlement; it must not delay the existing `DELETE /v1/auth` path.
Already-sent refresh may settle to its original slot while signed out, preserving
rotation, but cannot dispatch or publish into the ended session. Same-slot work waits
for settlement within the pre-dispatch budget. Exit uses the existing proxy drain
budget (10 seconds), without an additional custody wait; an interrupted rotation is
handled by the durable marker on next start.

ChatGPT disconnect closes its live generation, cancels work and removes the upstream
grant under the slot mutex after outstanding settlement. Keep the local bearer/ref
so existing tiers receive reconnect-required, not a missing/rejected-key card. Late
login completion must check its cancelled flow before saving; queued deletion runs
after any refresh settlement and the controller reports disconnect complete only
when deletion completes. Reset has the same scope for unusable custody. These are
local removal, not a claim of provider-side revocation; explain that distinction.

Confirmed **Remove this account from this device** also removes this feature's local
bearers, grant files and wrapping-key items for that account across its device slots
in this profile. Integrate with the existing signed-out check/native confirmation;
perform feature cleanup before completing removal and keep the recovery phrase last
so failures remain retryable in the picker. Retain key identifiers until their
idempotent deletion succeeds. No startup orphan sweep or old-preview cleanup is added.

Flow IDs and window incarnations prevent delayed login/store results from affecting
a closed/reloaded window; closing one window cancels only its own operations. For
inference, final validated response handoff to HTTP takes only a **short in-process
live gate** shared with retirement. Check session/connection generation and commit
the handoff under that gate, with no network, disk or keychain work inside it.
No successful completion/tool-call handoff occurs after retirement; a connected
request can still receive §7's cancellation error. Bytes handed off earlier can
arrive later and cause Bao to start complete tools; they cannot be recalled. The
proxy never hands off a partial call. This is a local publication rule, not
exactly-once delivery or a distributed cancellation protocol.

## 5. Fresh browser and device authorization

Offer browser and device-code flows independently, with cancel/retry and finite
expiry. Browser flow uses PKCE S256, unpredictable state and a single-use bounded
loopback callback, bound before opening the system browser. Retain the registered
redirect `http://localhost:1455/auth/callback`; port conflict is an actionable error.
Validate callback method/path/host/state and code, reject duplicates, and never echo
the query. Native code opens only its own stored URL. Device flow shows the fixed
verification URL and short user code to the owning window; polling credentials and
exchange verifier stay native. Honor polling interval, slow-down, denial and expiry
(maximum 15 minutes). Disabled device authorization does not disable browser login.

Compile the reference protocol in the native adapter: issuer `https://auth.openai.com`,
client `app_EMoamEEZ73f0CkXaXp7hrann`, `/oauth/authorize`, `/oauth/token`, device paths
`/api/accounts/deviceauth/{usercode,token}`, verification `/codex/device`, device
redirect `/deviceauth/callback`; inference is exactly
`https://chatgpt.com/backend-api/codex/responses`. Keep scopes and required account/
residency headers adapter-owned. HTTPS verification is mandatory; disable redirects,
environment proxies and automatic transport retries. Auth bodies are at most 128 KiB.
No caller chooses upstream URL, headers or credentials. These prior-source constants
and fixtures still require live certification before release; they do not establish
a generally supported third-party subscription API or account entitlement.

## 6. Models, full history and output translation

A selected tier is ordinary configuration:

```json
{"provider":"openai-compat","backend":"anyai","model":"chatgpt/gpt-6.1-sol",
 "base_url":"http://127.0.0.1:<port>/v1",
 "api_key_ref":"llm.key.anyai.chatgpt.<installation-id>",
 "traits":{"vision":true,"pdf_input":"none","web_search":false,"reasoning":"none"}}
```

`gpt-6.1-sol` is the fresh post-login suggestion, not a server default or entitlement
claim. Preserve saved/custom IDs and edits made during login. Require an explicit
model ID; no `chatgpt/default`, fallback model or provider switching. Curated catalogue
suggestions are labelled as such; custom well-formed IDs may be attempted and may
return model-unavailable. Connected is not proof that every model works.

V1 supports text, native function tools and base64 PNG/JPEG/GIF/WebP user images.
Reject remote image URLs, local files, PDFs, audio, image generation and native search.
Save codegen/classify/vision only: background jobs then use the same subscription
quota as chats. Existing CLI auto-connect precedence stays unchanged. **Search and
judge rows stay untouched.** A fresh ChatGPT-only user needs a separate search provider
(the current default is Gemini with its key); a working CLI search choice stays in
use when inference changes to ChatGPT. Do not silently reset it.

Translate leading system/developer text into `instructions`, joined by `\n\n` in
order; reject interleaved instruction roles. Preserve ordered user/assistant content,
user images, function calls and results as distinct Responses items. Empty assistant
text alone is omitted; tool-only `content:null` preserves its calls. Historical calls
remain valid when the current request offers no tools (Bao wrap-up). Preserve any
bounded nonempty call ID, including `autorecall_0`; downstream `tool_calls[].id` and
upstream `function_call_output.call_id` use the Responses **call_id**, never item IDs.
Do not send output item IDs back upstream. Reject structurally orphaned/duplicate
results, not history merely because its tool is no longer offered. History uses
Bao's existing reconstruction, including its replacement argument object after a
malformed-call error; do not require the original invalid response string to survive
that reconstruction or add argument-JSON/offer-list validation in the translator.

Flatten function definitions without renaming tools or rewriting schemas; retain
non-strict semantics explicitly. V1 accepts `n` absent/1, `tool_choice` absent/auto/
none; other choices, response formats and unsupported semantic options return 400.
An uncertified `reasoning_effort` is a 400, not silently discarded. `max_tokens` is
accepted as an **advisory** compatibility field, as on today's CLI route; no hard
upstream token cap is promised. Send `store:false`, full input and upstream SSE.
No `previous_response_id` or `prompt_cache_key` is added in v1.

**No encrypted reasoning or phase continuity in v1.** Do not request/echo
`reasoning.encrypted_content`; ignore bounded reasoning/phase metadata rather than
hiding it in tools or proxy state. On output, retain commentary, final-answer and
refusal text in output order, joining distinct nonempty text parts/messages with
`\n\n` into `message.content`. Tool calls have a separate ordered array; interleaving
and `phase` cannot round-trip through that Chat Completions shape. This and absent
reasoning continuity may affect quality/cache use; certify follow-up tool turns and
do not advertise parity with the previous Responses-specific implementation.

The direct route accepts downstream `stream:false` or omission only. Reject
`stream:true` with 400 before refresh/inference; CLI streaming is unchanged. Bao's
existing `anyai` JSON path has total timeout 590 seconds. Give the direct request
580 seconds overall, including queueing/auth, with idle bounded by the remaining
overall budget. It can therefore wait through long reasoning silence and return
its own deadline error before Bao's total expires. No downstream SSE/heartbeat
project is included. `/break` during JSON inference retains today's anyai limitation.

Parse upstream SSE with bounded state and correlation of response/item/content/call
IDs, ordering and finalized items. Require exactly one valid terminal response.
Completed `output_item.done` items are output authority; terminal `output` may be
omitted, null or empty. A nonempty terminal list must agree with the completed items
in order and supported fields; deltas alone never supply missing done items.
Unknown bounded metadata is ignored; unknown action/output types, conflicting IDs,
missing completion, malformed events or trailing events are protocol failures.
A completed response with no usable text/refusal or complete call is `empty_output`.

A complete call requires a nonempty unique call ID, name and an argument **string**.
Pass that string and name unchanged. Bao retains its existing argument normalization
and validation: an empty string becomes `{}`; nonempty malformed/non-object arguments
and unoffered names receive `is_error` under its malformed-call budget and are not
executed. Distinguish those completed model mistakes from
structurally partial calls. Never return calls from failed/incomplete output. A
recognized output-limit terminal may return text with `finish_reason:length`, all
calls suppressed; otherwise completed calls mean `tool_calls`, and text means `stop`.

Retain the 24 MiB request bound, four images of 5 MiB decoded each, and bounded
parsing (32 MiB response, 1 MiB event, 1,024 items/128 calls, nesting 64). Cancellation,
deadline and logout release request admission; handoff uses §4's live gate.

## 7. Errors and bounded recovery

Use fixed safe OpenAI-shaped errors, not raw upstream bodies/headers or token claims.
The route never returns 401. No error may tell Bao that the native local bearer is
a user API key to replace. Return these statuses for HTTP failures or equivalent
recognized upstream error events:

| Condition | HTTP / safe code | Existing Bao behavior |
|---|---|---|
| Wrong/foreign/unusable local bearer | 403 `local_connection_unavailable` | Stop; local reconnect/resave guidance |
| Native identity starting | 503 `starting` | Bounded retry |
| No grant, rejected refresh, rejected replacement access token | 409 `reconnect_required` | Stop; native sign-in |
| Locked/denied/corrupt custody | 409 `store_unavailable` | Explicit store recovery |
| Cancelled request / retired session or connection | 409 `request_cancelled` / `session_ended` | Stop; no retry |
| Slot busy before dispatch | 503 `auth_busy` | Bounded retry |
| Bad request/context length/unsupported option | 400 fixed specific code | Stop; correct request |
| Plan/quota exhausted | 402 `usage_limit` | Stop; plan-limit explanation |
| Entitlement/policy/residency refusal | 403 fixed specific code | Stop; explain access restriction |
| Model unavailable | 404 `model_unavailable` | Stop; retain chosen model |
| Admission busy / transient rate limit | 429 `busy` / `rate_limited` | Bounded retry |
| Upstream 5xx, transport failure, incomplete/invalid/empty output | 502/503 fixed code | Bounded retry |
| Actual overall request deadline | 408 `deadline_exceeded` | Stop; no retry |

Unknown upstream 4xx stays a safe nonretryable request/access error; do not infer
quota or account revocation from an unrecognized code. Include only a validated
bounded reset-time hint for recognized quota errors, no retry-after for exhaustion.
For transient failures, numeric retry-after is bounded to 1–60 seconds. Specific
private-endpoint error spellings remain certification cases.
If the downstream connection remains open, cancellation/fenced publication returns
the fixed nonretryable error, not a generic 5xx. If it has already gone, cancel the
work; no delivery guarantee can suppress the client's ordinary transport retry.

One rejected cached access-token revision gets **one refresh opportunity**: mark it
stale, retain the refresh grant and return 503 with retry-after 1. Bao's next ordinary
attempt refreshes before sending. A rejection of that replacement before successful
inference requires reconnect; do not start an internal resend loop. Late rejection
of an older revision cannot poison a newer one. Proactive refresh counts as that
request's opportunity; rejection of its freshly refreshed token ends recovery.

The proxy sends at most one inference per downstream HTTP request. Bao retains
ADR-005's maximum three attempts, 1/4-second backoffs and capped retry-after, including
incomplete replies. A 5xx before SSE is **not proof that no work was accepted**;
ordinary retries or a lost downstream response can repeat generation/quota use,
just as on existing Bao transports. No exactly-once promise is added. The 580-second
overall deadline returns nonretryable 408; Bao's own 590-second total-timeout handling
also remains nonretryable. Refresh-token ambiguity is different: §4 forbids replaying
an uncertain rotating credential exchange even when inference normally retries.

## 8. Minimal generic usage/accounting and replay

Emit standard `usage` **only when both input and output totals are known and coherent**;
otherwise omit the whole block, as the current proxy does. Cached/reasoning counts
are optional subdivisions, never extra totals. Thus old `llm@v1` already treats
unknown/partial totals as `usage.missing`, not measured zero.

Emit optional `"x_any_usage":{"version":1,"billing":"subscription"}` independently
of usage. Generic values are `metered|subscription|unknown`; malformed present metadata
means unknown. `OpenAICompatAdapter` copies this self-reported endpoint metadata into
`Reply.usage.billing`. It is a display hint, not verified billing or authorization.
Absence leaves the neutral Reply and historical interpretation unchanged. Subscription
means API token prices do not describe this cost, not that access is free/unlimited.

In `view.rs`, use the existing span-end usage: subscription/unknown marked turns,
or marked turns with missing counts, have `costUsd:null`; any such turn nulls the
run total and makes `run.priced` false. Keep existing first-request-model pricing
for metered/unmarked turns, including its mixed-model approximation and legacy
missing-count behavior. No per-turn model blob reads, historical repricing or CLI
cost cleanup. A namespace prefix alone is insufficient because `price_for` strips
prefixes. Update the generic unpriced explanation so subscriptions are not told to
add an API price. Check `effects.stats`, trace tables, `run_summary`, persisted
`agent_runs.costUsd` and Scheduled `lastCostUsd`; existing stored summaries stay as-is.

The Rust statistics half ships in desktop anyrt; `llm@v1` ships through the channel's
repo-space deployment **after** desktop release, then syncs to users. Prefer landing
and deploying backward-compatible accounting in an earlier release than UI enablement.
Bundling alone does not prove the new guest is deployed. An old guest ignoring the
marker can temporarily display a legacy API-price estimate for a priced model;
this is display compatibility, not a runtime safety/readiness/version gate. Ordinary
release/deploy ordering suffices; no coupled OAuth API or lockfile handshake.

Inference remains ordinary recorded HTTP, with only the local credential reference
in recorded input. Traces retain visible prompts/images/tool results/replies under
the existing policy. Replay uses recorded modules/effects, no proxy, login, refresh
or credential access. Preserve unmarked exact-Reply tests/parity and historical
summary semantics; strict replay needs no special ChatGPT mechanism.

## 9. Diagnostics, reuse and delivery

Keep the existing desktop diagnostics posture. The process already holds Any phrases,
API keys and connector OAuth tokens and has native crash reporting. Do not deliberately
log, trace or export this feature's tokens, callback queries, verifier/state, device
polling credentials or local bearer. Mark authorization headers sensitive, avoid
transport debug payloads and keep custody outside diagnostic export roots; test with
synthetic secret sentinels. Show only safe error/status metadata in telemetry.
This is **not** a guarantee that process memory dumps or user recordings contain no
secrets. No build-wide minidump disable, new recording hold or global observability
policy gate is part of this feature; broader process-memory policy is separate work.

| Prior work | Decision |
|---|---|
| Browser/device auth, token parsing, rotation/custody tests | Port into native any-ui types |
| Encrypted vault/OS key access, connection panel and flow ownership | Reuse suitable mechanics, adapt lifecycle |
| Responses parser/tool/image fixtures | Port, preserving Bao recovery and v1 translation limits |
| anyrt provider, broker interception, readiness/selection bridge | Do not carry forward |
| Navigation redesign, generic model transactions | Separate work |

Use a new versioned proxy custody directory/namespace; do not open/import/delete old
`bao-chatgpt-custody-v1` / `io.anyproto.bao.chatgpt.local-grant.v1` preview stores.
Preview users reconnect and explicitly resave. No automatic migration or cleanup
service is needed for the unreleased cohort. For the exact old `openai-responses` /
`openai-codex` row at `https://chatgpt.com/backend-api/codex` with ref
`connector.oauth.openai-codex`, preserve its bare model as an editable suggestion;
the explicit new save writes `chatgpt/<saved-id>`. Do not classify arbitrary
Responses providers as this preview. Old cleanup is a separate explicit action.

Prefer our proxy over a third-party proxy to avoid another installer and credential
lifecycle. V1 is desktop-only. Device-code approval is not a headless host: a future
host must own custody and consent transport while Bao still uses generic HTTP.
Do not expose desktop loopback remotely or invent a desktop CLI in this step.

After explicit acceptance: land the generic accounting change independently; implement
native custody/lifecycle and direct dispatch on fresh UI main; add translator and
connection-panel/generic-save integration; amend the owning UI proxy spec. Run focused
offline tests, then separately authorized synthetic browser/device/inference checks
on supported platforms. No real-user history, live quota use or credentials in the
normal test gate. Certification must cover:

- Fresh browser/device login without CLIs; denial/expiry/port conflict, keychain
  locked/missing/corrupt states, explicit authorization/reset and account removal.
- Stable restart/relogin/reconnect/disconnect credentials, background requests before
  any webview repair, foreign/equal-port bearer and startup: no rejected-row stamp
  or human key-entry card; correct new-ref host rebinding and responder selection.
- Concurrent single-flight refresh, crash/failed persistence/ambiguous rotation,
  cached-token rejection/recovery, rejection of fresh/old revisions, prompt logout,
  delayed custody completion and completed inference unaffected by another refresh.
- Handoff before/after retirement; cancelled window flows; late ordinary writes with
  metadata-only readback and explicit resave; existing duplicate-launch behavior.
- Actual Bao histories: tool-only null content, empty assistant text, tool-less wrap-up,
  synthetic call IDs, multiple calls/results, completed bad arguments/unoffered names
  reaching Bao's `is_error` path; text/image/refusal joining and phase/reasoning loss.
- Terminal output full/missing/null/empty, no usable output, partial/malformed/late
  events, bounds, long silence, JSON-only behavior, typed quota/access/model failures,
  ordinary bounded retries and actual deadline with no retry or partial tools.
- Known/partial/missing usage, optional billing marker, metered/unmarked compatibility,
  null subscription costs through summaries/UI, old-guest transition and replay with
  proxy stopped; preserved custom/default edits, separate search/judge and CLI behavior.
