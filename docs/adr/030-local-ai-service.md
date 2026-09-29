# ADR-030: Injected local AI generation

Status: **Accepted for the local experiment** (2026-09-28, explicit user approval).
Amends ADR-002 (effect catalog), ADR-005 (model transport), ADR-009 (embedding),
and ADR-023 (trace views). Distribution is not approved. The user subsequently
authorized bounded Codex live diagnostics on 2026-09-29, without paid-API fallback.

## Decision

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
(`run_cell` with `code`, `mock`, `mockref`; optionally `bash`), not harness tools.
Bao validates names, argument shapes, unique ids and final/tool consistency
before subcell execution. Thinking state is omitted; file parts are refused.
A validated final envelope can finish a Bao turn even when the provider's
finish reason is `other`; a reported length stop never executes a tool intent.
No partial output is executable.

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
