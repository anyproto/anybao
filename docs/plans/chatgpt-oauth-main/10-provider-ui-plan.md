# Step 10 — ChatGPT inside Provider, with compact connection controls

Activated 2026-11-04 from the user's two screenshots and instruction: ChatGPT
subscription belongs inside Provider settings, and its expanded settings are
overwhelming. Continue the existing paired feat/chatgpt-oauth-main worktrees.
Root verified all UI110/Bao35 delivery paths still match the Step 9 delivery
freeze before this step. No baseline reset, new branch, original-checkout edit,
commit or push. User continues to own live/GUI testing.

## Required sequence

One GPT-6 Astra max implementer owns this step. After its actual FINAL/source
freeze, run three independent GPT-6 Astra xhigh lenses: product/accessibility,
lifecycle/configuration, and security/integration. Root confirms/rejects every
finding, resumes the same implementer for confirmed findings, then repeats all
three reviews until no Critical/High/Medium finding remains. No extra agents.

## UI contract

1. Settings → Model has a single Provider section and a single Change flow.
   Remove the standalone ChatGPT subscription section and Set up ChatGPT /
   Close ChatGPT setup detour. Add ChatGPT subscription as a real choice in
   the same provider editor used for API providers and local CLI providers.
   Exactly one provider choice is active. Existing API/CLI semantics remain.
2. The saved summary presents Provider, Model and connection/account within
   Provider. OAuth must not get the custom/toml/config@v1 warning or an API-key
   Credentials jump as its main connection-management path. Keep that jump
   for ordinary API/CLI credentials. Do not repeat a second connection card.
3. For selected ChatGPT, show a compact account/status row, one model select,
   and standard Save / Cancel. Use existing select/menu/disclosure primitives.
   Suggestions remain conveniences, not an entitlement check. Retain explicit
   custom model ID input behind a Custom choice; show any existing custom ID
   faithfully and never coerce it to a suggestion. No model-catalog API work.
4. Connected and healthy is the quiet state. Account management exposes
   reconnect/change account and disconnect in a secondary menu/disclosure.
   Device-code login remains available as an alternate method. Signed-out or
   reauth-required has one clear Connect/Reconnect action. A pending login
   exposes its actual required code/open/cancel actions without hiding progress.
5. Hide recovery-only Check saved choice / Cancel saved choice from the healthy
   state. Preserve access when a configuration is incomplete, ambiguous or needs
   repair, with concise actionable labels. Do not remove read-only verification
   or own-apply cancellation semantics. Use one status surface, with short copy.
   A local Connected label must never imply the responder is ready; report a
   non-ready/remote/unsupported state honestly, even with a locally valid grant.
6. Avoid simultaneous redundant primary actions, model chips plus duplicate
   input, implementation jargon and long always-visible explanations. The saved
   model remains visible when editor is closed. A draft change never persists
   until Save; Cancel abandons the draft rather than disconnecting the account.

Target composition (existing Settings primitives, not new design-system chrome):

    Provider
      Provider    ChatGPT subscription        Change
      Model       <saved model>
      Account     Connected on this device   Manage
      <one concise non-ready notice only when needed>

    Provider editor (within this flow)
      <existing API choices + ChatGPT subscription + existing local choices>
      <selected ChatGPT account/status + secondary management>
      Model       <select; Custom reveals a bounded ID field>
      <actionable pending/error/recovery state when present>
                                  Cancel     Save

The implementer may refine component boundaries to fit existing primitives,
but must preserve this information hierarchy and functional invariants.

## Owning source and contracts

Primary files: SettingsModel.tsx/tests, ModelPresetChooser.tsx/tests/stories,
ChatGptModelSetup.tsx/tests, ChatGptConnectionPanel.tsx/tests, ModelReadinessStatus.
Audit shared consumers: BaoWelcome, SettingsCredentials, credential request
cards and any existing stories. Avoid duplicating the OAuth controller or
creating an alternate configuration-save path just to share a form.

Read applicable AGENTS and frontend playbooks, existing specs PR-219,
2026-10-13-local-ai-model-settings, 2026-10-23-current-model-options, and
2026-11-03-fresh-chatgpt-oauth. Update the latter's scope/acceptance and the
handoff runbook's manual navigation. Source/data transport contracts were
verified in Steps 7–9; this step adds no endpoint or native custody behavior.

Keep current api/applyModelConfiguration, retryModelConfiguration and
abandonModelConfiguration behavior: one atomic fresh batch, read-only exact-ID
retry, additive own-ID cancellation, native write ownership/quarantine and
lease fencing. Preserve custom search/judge, CLI-owned search transitions,
CLI port repair/auto-connect fences and the first-chat announcement contract.
Avoid losing intent on provider-tab switches or letting stale async completion
close or overwrite a newer draft. Keep pending disabled behavior consistent
across the shared form and don't nest forms or trigger save from a secondary
button. Authentication remains an independent local connection; no token import,
secret synchronization, new telemetry fields, localStorage or raw network path.

## Design and verification boundaries

Use the repository deliver-frontend-feature skill and relevant component/
storybook instructions. The user's screenshots and requested correction are
the authoritative layout direction for this iteration. The OAuth composition
has no dedicated Figma node (already recorded in the owning spec). Do not
invent a Figma match or claim visual acceptance. Reuse the approved isolated
worktrees/baseline and the user-requested three-lens panel; no extra review
agents. Keep existing native/guest source unchanged unless a concrete blocker
is reported to root first.

No app/browser/CUA execution, real profile/keychain/CLI discovery, live OAuth,
inference, wallet/recovery reads, shared deployment or release. The user has
now signed into the prepared profile: treat it as live user state, not a
disposable scratch fixture. Do not inspect, reset, restore, copy or modify it.
Do not rebuild over the potentially running prior Any.app in Step 10.
Use fake auth/native adapters and SyncStore fixtures for automated component
interaction and accessibility checks. Stories use synthetic account labels.
Live visual/Light-Dark acceptance remains user-owned, as in previous steps.

## Checks and completion evidence

- Meaningful interaction regressions for one provider flow, selected OAuth,
  connected/signed-out/reauth/pending/remote/error states, keyboard model/custom
  choice, Save/Cancel, pending saves and newer-draft/session transitions.
- Shared-consumer checks retain welcome, Credentials, API and CLI behavior;
  validate real existing apply/retry/cancel paths through controlled seams.
- Accessible names, keyboard/focus behavior and existing axe conventions;
  stories for the compact states with honest pending-design metadata.
- Run affected UI tests, typecheck and pnpm lint:push (task-pinned toolchain;
  unset CARGO_TARGET_DIR only for that gate). Build relevant Storybook/static
  frontend outputs where appropriate without launching any app. No broad
  native/Python suites for an unchanged native/guest tree.
- Keep evidence and probes in /private/tmp/anybao-oauth-main-evidence/step10-*
  or an assigned scratch directory, not source/spec indexes. Record full
  changed-path inventories, diff from Step 9 and precise commands/limits.
- Deliver actual FINAL with source frozen before any review agents start.

After Step 10 clears, root will write the next narrow build/handoff plan for
an updated artifact that preserves the user's current connection and test
profile. That later step must not overwrite a running app or blindly replace
the source/credential identity checks. Do not expand Step 10 into launcher
migration or profile operations.
