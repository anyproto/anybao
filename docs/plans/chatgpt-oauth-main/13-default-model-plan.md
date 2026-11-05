# Step 13 — preselect a model for fresh ChatGPT setup

## Request and evidence

The user supplies two identical screenshots of Settings → Model → Provider:
ChatGPT subscription is selected and connected, but Model shows “Choose a model”
and Save is disabled. They request a default, suggesting GPT-6.1 Sol, and ask
whether ChatGPT supplies one. Source confirms `modelDraft` in the shared setup
hook uses the stored model or an empty string; login does not supply a model.

Official OpenAI documentation fetched 2026-11-05 documents a separate model
catalog and an explicit model ID for inference, not a default in the OAuth
result. GPT-6.1 Sol is a documented model. This step uses `gpt-6.1-sol` as Bao's
UI suggestion, not an entitlement claim or an OpenAI-selected default:
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/api/docs/models/gpt-6.1-sol

The newly documented token-sharing/public-Responses flow is a separate protocol
from this branch's existing Codex OAuth path. Do not silently migrate endpoints,
tokens, scopes or auth registration as part of this small UI change. No account
catalog/network call is required for a local default suggestion.

## Behavior and implementation

1. Introduce one explicit default constant beside the existing model suggestions.
   The shared Settings/welcome setup draft uses `gpt-6.1-sol` only when there is
   no saved choice and no active user draft. Do not depend on list ordering.
2. Preserve stored model IDs verbatim, including custom IDs. Preserve user edits,
   including deliberate empty/invalid custom input, through connection/status
   updates. Cancel discards a draft; a subsequent fresh choice can use the default.
3. This is preselection only. Existing Save/Apply remains the sole configuration
   mutation. Connection/readiness/loading/session/selection-lease gates remain;
   signing in must not auto-apply, send a message, import CLI tokens or alter
   other provider/model settings. Late config hydration must retain the existing
   saved choice when there is no user draft. Keep current session/space fencing.
4. Reuse existing components and owning fresh-OAuth spec. This is a value/default
   change, with no layout, theme, primitive or new server/API work. The user's
   screenshots plus existing no-dedicated-Figma-node contract govern the surface.
5. Add a failing integration regression first for connected fresh setup with no
   stored model; verify default displayed, no writes before explicit Save, and
   saved tiers use the default. Cover shared welcome behavior and relevant saved,
   edited, disconnected/loading and cancellation preservation with focused tests.
   Run affected UI tests, typecheck, typed lint, formatting, required lint:push
   and applicable static story checks. Do not rerun unrelated Rust/Python suites.

## Runnable handoff

After source checks, package a distinct `Any Model Preview.app` using a scratch
productName-only overlay, pinned toolchain, paired ANYBAO_DIR and ad-hoc signing.
Preserve all three previous complete bundles (`Any.app`, Provider Preview,
Recovery Preview), old anyrt, SDK, kernel and all deployment/namespace inputs.
Use a bounded non-release environment; never execute an app or helper binary.
Host platform only: macOS arm64/minimum14 ad-hoc debug app, no installer/updater
publication/notarization. macOS x64, Linux and Windows packaging are out of scope.

Use the strict existing renderer-only v1 metadata-update contract, with its
single prior endpoint equal to the exact Step12 Round2 recipe destination and
fixed fields from that public recipe. Native/Bao source inventories must remain
identical. Generate a new immutable recipe/digest and verify Node source/bundle
ordering, artifact signature/architecture/kernel/SDK/namespace, all old bundles
and both update/no-op paths on fresh fake roots. Do not weaken/update helper code
to accommodate a new build. Update the runbook with exact user-only update/resume
commands. Keep earlier candidates/recipes/proofs immutable.

## Execution and review

Reuse the existing paired feature worktrees. Their complete initial inventories
match Step12 delivery exactly (UI124 and Bao40); originals remain untouched.
One GPT-6 Astra max implementation agent owns product/test/spec/runbook edits.
Root owns this plan, progress, source freezes and any build preparation. After
actual implementation+handoff FINAL, three GPT-6 Astra xhigh reviewers independently
review product/integration, lifecycle/state and security/artifact. Root confirms
or rejects each finding; resume the same implementer and repeat all three until
no C/H/M remain. The existing user's workflow governs the skill's review panel.

The protected `/private/tmp/anybao-oauth-main-handoff` and every child remain
outside all agent reads/stat/list/verify/update/launch operations. No real
Keychain/CLI credentials/profile/process inspection, GUI, OAuth, inference,
installed-app changes, commits/pushes/releases or original checkout edits.
Synthetic tests/static artifacts only. User-owned live smoke overrides the
playbook's automatic live-smoke suggestions. No additional approval is needed
for this requested narrow continuation within the existing spec/worktrees.
