# Step 7 — Port fresh ChatGPT OAuth to current Bao

Started 2026-11-03. The owner requested reimplementation on current anybao and
any-ui main, preserving the new Codex/Claude CLI options alongside fresh OAuth.

## Isolated bases and scope

- anybao: `fb1de0f0732e296693116b1b0e80f64d7485ad1d`.
- any-ui: `52683f5feffefa74435cfc9c02483937e40bb281`.
- Both branches: `feat/chatgpt-oauth-main`.
- Paired worktrees: `.worktrees/chatgpt-oauth-main/{anybao,any-ui}` under the
  original anybao checkout, preserving the desktop's sibling Cargo dependency.
- Original checkouts, unrelated Android work and the old OAuth worktree remain
  untouched. This is a selective port, not a transplant of their dirty diffs.

The current UI uses an `any-ai` local HTTP proxy and CLI subprocess sessions.
Those credentials and inference paths remain intact. Fresh OAuth is a separate
native grant and direct Responses provider; it never imports or recovers another
application's tokens and never silently falls back to a CLI or API key.

## Sequence

1. Step 7: runtime authentication, custody interfaces, protected broker route,
   Responses codec, configuration/readiness contracts and offline regressions.
2. Step 8: desktop custody/session integration and current UI onboarding,
   credentials and model selection, coexisting with CLI discovery/auto-connect.
3. Step 9: cross-repository validation and a reproducible local run handoff.

For each step: one GPT-6 Astra max implementer, actual FINAL and frozen source,
then three independent GPT-6 Astra xhigh reviews. Root adjudicates every finding,
resumes the same implementer with confirmed findings, and repeats until no
Critical/High/Medium issue remains. Do not advance implementation across steps
before the preceding review gate passes.

## Runtime implementation

1. Read current runtime/guest contracts and retained OAuth source. Reimplement
   missing wiring against current main; retain newer judge, search, tools,
   configuration, CLI proxy timeouts and trace behavior. Do not port Android work.
2. Preserve fresh browser PKCE and device-code login; scoped local custody;
   rotating access/refresh pair; cross-process serialization; durable ambiguous
   refresh handling; cancellation/logout/session fencing; token-free events.
3. Restore the native-only `connector.oauth.openai-codex` credential family and
   exact ChatGPT Responses allowlist. The guest/webview/sync/export/trace must
   never receive access or refresh tokens. No configurable bearer destination.
4. Restore `openai-responses` / `openai-codex` as a distinct provider. Preserve
   streaming completeness, phase/refusal/function/reasoning provenance, safe
   auth retry, stateless history and unknown subscription monetary cost. Carry
   the live-tested completed-items fallback when terminal output is absent,
   null or empty, without weakening incomplete-tool rejection.
5. Define host/UI integration APIs for the next step and target/config readiness.
   Preserve existing `anyai` and API-key semantics, explicit provider selection,
   all relevant tiers and search configuration. Use current model contracts;
   never let default CLI auto-connect replace an explicit OAuth choice.
6. Update documentation without colliding with current ADR numbers. Historical
   September plans are context, not proof for this new source. The old temporary
   snapshots/tool environments no longer exist.

## Validation and freeze

Use repository-pinned Rust 1.99.0 and Python >=3.14, a task-owned virtualenv and
build output. Record the pre-change baseline separately from implementation
results. Run focused auth/codec/config tests, offline Python replay checks,
runtime tests for applicable features, canonical clippy and format checks.
`ANYBAO_LLM_PARITY=replay`; no live-provider parity or integration server unless
it is an explicitly created empty scratch server. Sandbox loopback failures
must be distinguished from regressions.

The implementer reports exact changes, test evidence and remaining limitations,
then stops editing. Root freezes the delta and assigns three read-only lenses:
credential custody/security; protocol/lifecycle/correctness; current-main
integration/regression coverage. Review artifacts stay in task scratch storage.

## Execution boundaries

Do not read real profiles, auth stores, keychain items, wallets or tokens. Do not
launch the GUI, browser, CLI authentication, live OAuth or inference. Only
synthetic fixtures, fake custody and scratch loopback services are allowed.
The user owns interactive testing. No commits, push, deployment, release or
installed-app replacement. Product edits belong to the implementation agent;
root owns this plan, validation environment and review adjudication. Coordinate
heavy test/build lanes to avoid duplicate work or resource contention.
