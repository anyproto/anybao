# ADR-030: Injected local AI generation

Status: **Accepted for the local experiment** (2026-09-28, explicit user approval).
Amends ADR-002 (effect catalog), ADR-005 (model transport), ADR-009 (embedding),
and ADR-023 (trace views). Distribution is not approved. The user subsequently
authorized bounded Codex live diagnostics on 2026-09-29, without paid-API fallback.

## Decision

### Requested speed amendment — 2026-09-30

The user approved Standard / Fast through the same local settings/model controls.
Reuse any-ai Speed and settings types; record and freeze speed in ai.resolve and
every subsequent model/search request. Legacy resolved records with missing speed
migrate to Standard. Fresh selections inherit saved speed when omitted;
explicit tier speed wins. Guest select_model preserves omitted speed for the
selected model, and explicit None resets Standard. Image generation pins Standard
because controller speed is not image-generation speed. Existing HTTP tiers,
credentials and published shared programs remain unchanged.

Only an explicit informed user request may enable premium Fast: explain Codex's
higher quota/credit usage or Claude's separately billed usage credits, and ask if
consent was not already given. “Be quick” is not consent. Catalogue support is not
entitlement or guaranteed effective speed. Native validation remains authoritative;
no model/provider substitution, automatic setting retry or policy bypass. Strict
replay consults neither live preferences nor providers. Add fixture/kernel tests;
no paid Fast test or automatic activation is authorized by this implementation.

### Chat-requested model selection — 2026-09-30

Accepted by the user for the local experiment, including direct application on
an explicit chat request without a second confirmation. Add recorded reads
`ai.settings.get` / `ai.models` and mutation `ai.settings.set`; the setter has
its own `ai.settings.write` capability, not inference's `llm.chat`. It updates
the same desktop profile preferences as Settings via revision-checked atomic
persistence and the same native change event. It never edits synced tier rows,
API credentials, admission policy, executables or shared overlays.

The setter accepts expected revision + a complete neutral resolved selection.
Validate against the selected harness's current non-generating model catalogue;
honor cancellation/deadline before commit. Replay/mock uses the recorded result
without executing the mutation. Keep the current kernel's resolved tier frozen;
new runs observe saved defaults, while explicit tier overrides still win.
Retain unknown/failed/stale outcomes rather than guessing a model or retrying a
save. Guest methods live only in local_ai:llm@v2; update the private loop's
instructions to permit this specific user-requested settings operation while
keeping legacy API-model switching in Settings. Explicit user intent is enforced
by agent instructions under Bao's existing permissive grant policy, not by
claiming that arbitrary guest text proves human authorization.

### Autonomous media/search experiment — 2026-09-30

The user approved sequential local implementation and testing without milestone
review pauses: image understanding, image generation, web search, and file
reading, maximizing reuse of existing tools. The media contract is any-ai
ADR-003. Reuse ADR-020 `llm.read` / `file_content` and ADR-026 blobs / `attach_file`;
do not load binary attachments into the main chat prefix. Ordinary generation
retains its no-provider-tools contract. New search/image-generation operations
are separately restricted, recorded and cancellable, with no paid fallback.
Only the approved private test spaces/dev app may change. Published shared v1
sources stay untouched; unsupported formats/providers fail honestly.

The earlier text-only limits below describe the baseline and are superseded
only for explicitly implemented and tested capabilities in this amendment.

The user subsequently approved an explicit cross-harness image route: keep
Claude `sonnet` / `xhigh` as the device's text default, and configure only
`llm.tier.local_image` with `provider: any-ai`, `harness: codex`, a 240-second
deadline and an 8-MiB output ceiling. Its model/effort follow the Codex preference
through recorded `ai.resolve`; no default is changed and this is not fallback.
Reuse the existing `llm.image_generate` → `ai.image_generate` → Blob →
`attach_file` path. There is no native Claude image generation. Provider image
cost/total usage are unknown, not zero; strict replay returns the recorded blob
without a provider call. Configure only the private dev account and seed file.

The Codex search follow-up reuses `llm.search` and the recorded `ai.search`
effect without guest changes or shared-source deployment. A dedicated private
`llm.tier.local_codex` row explicitly selects Codex, symmetrical with the existing
`local_claude` diagnostic route; its model/effort resolve from host preferences.
`llm.search(query, tier="local_codex")` can therefore be tested without changing
the ordinary `local_codegen` route or the Claude text default. Replay must run
with no AI service and return the recorded answer/sources. This does not enable
search implicitly inside normal generation or change existing v1 HTTP tiers.

Desktop/serve run headers must include the entry arguments before the first
streamed record, matching the CLI contract. A live synthetic media replay
exposed that the existing serve path omitted them; this amendment repairs both
normal and inline/control run recording. Existing incomplete headers are not
rewritten. Media/search effects remain replayable without an injected service.

Bao keeps its agent loop, prompt composition, memory, tools and cell execution.
`any-ai` supplies bounded text/JSON generation through a user-installed CLI.
It is not a raw provider API: files, exact token caps, native tool calls and
provider reasoning-state round trips are unsupported on this route.

`anyrt` exposes an injected `AiService` and `Services { ai }`.
`start_with_services(cfg, services)` installs the service in every run broker;
`start(cfg)` remains available with no service. The trait reuses `any-ai`'s
neutral request/result types instead of maintaining a second wire contract.
An implementation for `AnyAi` wraps the broker's existing interrupt flag in
`CancelToken`. A sibling path dependency is intentional for these local branches;
it must become a pinned published revision before distribution.

## Recorded effect

`ai.generate` is class `read`, capability `llm.chat`, and uses the ordinary
normalize/key/capability/replay/mock/execute/record pipeline. Its input and output
are the serde shapes of `GenerateRequest` and `GenerateResponse` (snake_case,
including nested `limits`). An explicit nonempty `harness` is required for Bao.
Unknown fields, invalid bounds and malformed results are errors. The host clamps
the request deadline to the run deadline and uses the run's one-shot interrupt.
The optional service is consulted only in execute: replay and mocks work without
a provider, executable, login or installed service. No automatic retries or
provider fallback follow an accepted generation.
The existing missing-program sync retry is also suppressed once a run attempted
live local inference; replay and mock effects do not count as provider attempts.

Authentication and executable paths are trusted host configuration, never guest
inputs. Prompts and validated results belong in the existing private local trace;
credentials, provider diagnostics and partial generations do not. The trace view
recognizes this effect and the enclosing neutral `llm.chat` result, including
model/harness provenance. Unknown usage/cost must not be displayed as measured
zero or priced as an ordinary API call.

## Guest contract and versioning

Add `llm@v2` for the local route; delegate existing HTTP tiers to `llm@v1`.
The new route is selected by a tier row such as
`{provider: "any-ai", harness: "codex", model: "…"}`. A local profile has
explicit text-only capabilities and a conservative configurable context budget;
it cannot inherit vision, sampling, signed thinking or token-limit promises from
the model name. Unsupported options fail before inference. The existing tier
config store remains authoritative; service injection does not overwrite it.
Numeric configuration bounds accept only finite integral JSON numbers, whether
decoded as integers or floats; the guest normalizes them to integers before
publishing the context profile or constructing `ai.generate` limits. Booleans,
strings, fractional numbers and out-of-range values remain configuration errors.

The guest serializes neutral conversation parts as data and requests a strict
JSON envelope for the next reply. Tool intents use exactly the offered tools
(`run_cell` with `code`, `mock`, `mockref`, `full_output`; optionally `bash`), not harness tools.
Bao validates names, argument shapes, unique ids and final/tool consistency
before subcell execution. Thinking state is omitted; file parts are refused.
A validated final envelope can finish a Bao turn even when the provider's
finish reason is `other`; a reported length stop never executes a tool intent.
No partial output is executable.

### Upstream prompt update — 2026-09-30

The user approved updating the local experiment to Bao main (PR-63,
`a3d42f3`). The private loop retains the upstream prompt composer, on-demand
skill index, compact tool inventory, teach-on-failure digest, own-chat guard,
cell prelude and recorded context/effect summary. Local argument validation,
route freezing, missing-usage accounting and no paid fallback remain intact.
`full_output` is a strict boolean in both the reply facade and executor guard;
it changes digest rendering only, never execution permissions or output caps.

The facade accepts a system string or a list of strings. HTTP tiers delegate
the original value to the updated v1 facade. For local generation, nonempty
blocks join in their original order with two newlines before the existing
transport instructions. any-ai still accepts one system string: this preserves
the stable-first prefix but does not promise Anthropic API cache breakpoints
through the CLI. Provider-reported cache usage remains the only measured evidence.
The existing default-off Claude formatting repair is unchanged.

For this update only, the user explicitly approved private test copies of the
latest agent and connector sources if the official hidden sources remain old.
Keep `_local_ai` limited to its two programs; use separate fresh private spaces
for the upstream snapshots, point only the dev profile's aliases at them, and
retain the official configuration for rollback. These pinned snapshots need
manual refresh until the profile returns to the official synced sources. Never
write the shared production spaces or change the normal installed app.

Add `toolcaller@v2` with the new facade and validation, preserving the frozen
v1 programs. A trusted host `agent.program` option selects the chat program;
the default remains `agent:toolcaller@v1`. Local experiments explicitly select
v2 and deploy only into an isolated test overlay. Do not rewrite published v1
objects or publish to production.

### Shared-source amendment — 2026-09-29

Explicitly approved by the user: keep the official hidden `agent` and
`connectors` spaces as the normal read-only, synced sources. Deploy only the
two experimental programs from `repos/_local_ai` to a separate private space,
aliased `local_ai`; select `local_ai:toolcaller@v2`. Cross-repo dependencies
must use `agent:` explicitly, never copies in the experimental space. The
local facade stays a defining-space import; model-facing subcall instructions
name `local_ai:llm@v2`. Skills and tool inventories continue to use the normal
`codeSpace = overlays["agent"]` path, with user-space overrides unchanged.

No mirrored upstream programs/skills, startup redeployment, or shared-space
writes. Ordinary source updates arrive through the existing sync/resolver
mechanism. The experimental loop itself remains a fork and needs explicit
updates until published upstream; it does not inherit future loop changes.
The UI recognizes the v2 program under the approved `local_ai` alias as well
as the earlier isolated `agent` alias. Unknown programs stay on the legacy
readiness path; no arbitrary suffix-based capability inference.

The v2 loop defaults to a dedicated `local_codegen` tier. Only configure
`llm.tier.local_codegen` for the experiment; leave `codegen`, `classify` and
`vision` alone. Frozen helpers such as enrichment and subagents still use v1
and are not migrated by service injection. Explicit `tier` arguments remain
available for testing HTTP delegation through v2. This is not an account-wide
ban on paid providers: preexisting HTTP-backed helpers retain their own routes.
Use a fresh test account without API credentials and do not invoke those helpers
in a local-only smoke test.

## Lifecycle and device scope

### Claude local test amendment — 2026-09-29

The user authorized enabling Claude in the local debug desktop and short live
checks through their existing provider-owned login, acknowledging possible
metered usage. The host may add a dedicated `llm.tier.local_claude` row for
explicit `tier: "local_claude"` diagnostic runs. `local_codegen` stays on Codex;
v1 tiers, shared hidden source spaces and the private guest programs remain
unchanged. The library's Claude-scoped metered-risk opt-in must not permit
metered Codex authentication. No automatic fallback or distribution approval.

The desktop owns one shared `Arc<AnyAi>` for Bao and UI requests. A standalone
CLI may opt in explicitly; default startup performs no harness discovery or
generation. Claude remains disabled unless the trusted host explicitly accepts
both provider and metered-auth policy; no token-file parsing or custom login.

All generations run on the host executing the Bao run, including pinned jobs.
Switching Bao's active device never transfers CLI credentials or in-flight work.
Unavailable local harnesses fail actionably without choosing a paid API or
another device. Account-scoped model selection is not evidence of local readiness.

Service shutdown prevents new generations and cancels/drains accepted local AI
calls before releasing their process owner. Reuse the existing live-run registry
and interruption machinery, including queued work and run deadlines. No polling
bridge thread per request, detached provider worker, daemon or public port.

## Verification

### Local settings amendment — 2026-09-29

Approved with the Any UI settings feature: add recorded `ai.resolve` (class
`read`, capability `llm.chat`) to resolve optional `{harness, model, effort}`
using the injected service's host-local any-ai preferences. It performs no
inference or discovery. The result is `{harness, model?, effort?}`. Replay/mocks
do not consult the host.
The local v2 facade resolves each tier once per kernel/run and retains the
resolved tier, including an absent model (CLI default), through the tool loop.
`ai.generate` keeps its explicit harness contract and does not reapply defaults.
Existing explicit tier choices win. A local tier may omit harness to follow the
device preference. Only the private local_ai overlay is updated for this test;
official shared sources and v1 HTTP tiers remain unchanged.

The accepted model/effort extension records requested effort with that same
frozen route. Explicit tier effort overrides the selected model's host default.
Tier effort is optional and accepts only the neutral values `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; unknown strings and
non-string values fail before generation. An effort requires a resolved model;
an absent model keeps the CLI's own effort default. Every `ai.generate` in the
tool loop carries the frozen effort unchanged. Provider-supported effort checks
belong to any-ai at live admission, not the guest or replay: no catalog lookup,
clamping, or provider substitution can alter a recorded choice. This is requested
effort, not a claim about measured reasoning or provider account limits.

- Mock service: success, unavailable, denial, bad input/output, timeout and cancel.
- Strict replay and loose mock: zero service invocations, same output/error.
- Guest: tool/result/final cycle, mock arguments, optional bash, unknown tools,
  malformed code, duplicate ids, unsupported files/options and missing usage.
- Trace: local request/result/model visible; unknown subscription cost stays null.
- Lifecycle: shared provider slot, cancel while queued, shutdown with active work.
- Default HTTP tiers and v1 programs retain their tests.
- Normal gates never call real models. Non-spending CLI compatibility is separate;
  paid end-to-end parity requires explicit operator opt-in and remains unproven
  until run. A passing fake-service suite is not a provider compatibility claim.
