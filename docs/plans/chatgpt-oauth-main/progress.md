# Fresh OAuth port to current main

Started 2026-11-03. See [Step 7 plan](07-runtime-port-plan.md) for pinned bases,
scope and the owner-requested implementation/review workflow.

| Step | Scope | State |
| --- | --- | --- |
| 7 | Current Bao runtime port | Complete; Round 2 cleared all three lenses |
| 8 | Current desktop and UI integration beside CLI providers | Complete; Round 2 cleared all three lenses |
| 9 | Cross-repository validation and local run handoff | Complete; Round 2 cleared all three lenses |
| 10 | ChatGPT within Provider and compact connection controls | Complete; Round 1 cleared all three lenses |
| 11 | Separate preview and profile-preserving user update | Complete; Round 1 cleared all three lenses |
| 12 | Explicit secure-store recovery and runnable preview | Complete; Round 2 cleared all three lenses |
| 13 | Default ChatGPT model selection and preview | Complete; Round 1 cleared all three lenses |

Original main checkouts and the old OAuth worktree are preserved. Neither the
historical live acceptance nor old review results validate the new port.

## Current-main baselines

- Rust 1.99.0; Python 3.14.7 via locked uv environment; Node 26.8.1;
  repository-pinned pnpm 12.11.2.
- Bao kernel built. Offline Python: 1,273 passed, 16 skipped, 16 integration
  tests deselected. Rust default library: 334 passed, 1 ignored.
- UI typecheck passed. Full unchanged-main UI unit baseline: 31,179 passed,
  3 failed across 2,024 files. The three affected table/editor files then passed
  together with one worker and no heavy concurrent build: 249 tests. Load or
  ordering is suspected; the cause is not proven. No unrelated source fix.
- Task-local tools require sandbox escalation to execute; an initial pnpm
  attempt fell through to the older system binary. It is not a source failure.

## Step 7 implementation verification

The implementer reached FINAL after passing Python replay (1,496 tests), Rust
default (442 library and 5 additional tests), Rust shell (466 library and 5
additional tests), focused final identity/view regressions, Clippy for both
feature sets, Ruff and formatting. Native desktop custody, UI integration and
live acceptance remain later steps; these results are not end-to-end approval.

Round 1 produced no Critical/High findings. Root confirmed three Medium issues:
stale configuration operations can damage newer intent; a slow readiness query
can block ordinary presence; and the compatibility check admits an unsupported
deep-research provider. The same implementer will repair them before another
three-lens review. The security lens reported no Medium-or-higher finding.

The same implementer repaired all three and reached FINAL again. A fresh model
selection now uses one complete batch; retry only verifies the expected apply;
cancellation adds a marker for its own apply. Readiness runs on one bounded
background worker and expires by query start time. Search compatibility is
checked separately for web search and deep research.

Final repair gates passed: Python replay 1,512 tests (16 skipped and 16
integration tests deselected), Rust default 445 library plus 5 integration,
Rust shell 469 library plus 5 integration (one ignored in each), both Clippy
variants, Ruff, formatting and diff checks. A final test-only timing adjustment
was covered by the full shell run and a focused default publisher rerun.

The desktop's pinned Any v0.3.4 binary embeds any-sync-sdk v0.6.4, whereas the
older sibling server checkout uses v0.4.3. The atomic batch, additive cancel,
reset and transport assumptions were verified against both SDK sources and
the exact packaged HTTP handler. No live server or profile was needed for
that source verification. Round 2 must still independently review this repair.


## Step 7 review gate and Step 8 activation

All three Round 2 reviewers reached FINAL with no Critical/High/Medium issue.
Root read their evidence and independently verified that all 32 frozen paths
remained identical (manifest ffec44cfc7e34a10963998dc350d4c8afae1a8cbe8ec12a6186bccf556583fb0).
The prior three Medium findings are resolved. Independent reviewers exercised
configuration interleavings, incomplete stream cases, native auth lifecycles,
the actual publisher, worker expiry/backpressure/drop, and retained CLI/API,
judge, search, history and accounting behavior. No live acceptance is claimed.

Step 8 now follows [the desktop/UI plan](08-desktop-ui-plan.md). The paired UI
was still clean at activation. The runtime gate does not validate the native
vault, IPC/window boundary or product integration that this step adds.

## Step 8 implementation progress

The feature spec is in paired any-ui at
`docs/specs/2026-11-03-fresh-chatgpt-oauth.md`. Native custody, session/window
integration and generated IPC are being adapted to current main. An intermediate
`cargo check --tests` passed, followed by 39 IPC export tests. Linking required
an environment-only SwiftPM output-directory repair in task Cargo OUT_DIR;
dependency and product build scripts were not changed for it. Final native/UI
checks and the three-lens review remain pending.

The current desktop design coordinates explicit intent across workspace windows
and keeps a native-owned model POST alive through renderer disposal. Uncertain
writes are durably fenced against silent replay; exact readback or a proven
backend reboot governs recovery. The first welcome waits for matching active
responder readiness. These are implementation obligations, not yet review or
live acceptance results.

Intermediate checks now include 47 passing native ChatGPT tests and 89 passing
affected UI tests across Model/Credentials settings, model API, the connection
panel and model setup. Typecheck passed. The affected run caught and repaired a
real Cancel visibility issue during login acquisition as well as stale fixtures.
Full UI/native/lint gates and the independent review gate are still pending.

The first full UI run finished with 31,321 passes and 21 failures in six files.
Root confirmed that main's optional `key` field for ordinary model rows must
remain supported; OAuth strict validation must not redefine that contract.
The implementer restored ordinary lookup, kept raw OAuth/target ambiguity
strict, bounded only relevant model rows, and removed an unnecessary heartbeat
subscription for ordinary consumers. Affected credential-save fixtures were
also updated for the authoritative readback. All six files and new regressions
then passed together (355 tests, nine files). Final full gates remain pending.

## Step 8 implementation FINAL

The Astra max implementer reached FINAL and froze product source after the
second full UI run passed: 31,348 tests across 2,033 files. The safe native
suite passed 869 tests, with five live tests ignored and four real-keychain
tests explicitly excluded. Both Clippy variants, typecheck, affected typed
lint, all 18 selected lint:push tasks and formatting passed. All 39 generated
IPC exports match exactly; the native test binary targets macOS 14.0.

Thirteen intentional guard mutations failed by assertions and were restored
byte-for-byte. A new shutdown test initially poisoned an unrelated process-wide
meeting flag; isolating those unrelated test side effects preserved production
shutdown order and made the existing meeting assertion pass unchanged. The
implementation report and exact commands/logs are in
`/private/tmp/anybao-oauth-main-evidence/step8-implementation-report.md`.

No Bao product source was changed during Step 8. Root now freezes the paired
inventories for three independent Astra xhigh lenses. This is not yet a review
gate pass or a runnable/live acceptance claim; Step 9 remains pending.

## Step 8 Round 1 adjudication

All three Astra xhigh reviewers reached FINAL. Root verified both complete
changed-path inventories remained byte-identical through review: UI 103 paths,
manifest 4f4c5a37f7d63e02a08cad383a77091dc78352a7602942aaba0a22f7f6029642;
Bao 33 paths, manifest ce7e5f8495c77e04c5d53309a47d7ee74e318e87567bc5d6e5cc3efd281d3822.

Root confirmed one High: native selection authorization rejects the dotted
space IDs used by the packaged SDK and committed wire fixtures. This blocks
OAuth setup and existing desktop API/CLI model writes. Lifecycle and integration
reported the same root cause; it is counted once. Root confirmed one Medium:
a queued acquisition captures its epoch after window/session retirement, so it
can create orphan or stale intent. Exact-code scratch probes reproduced both,
including a controlled Tokio blocking-queue interleaving.

The security lens found no separate Critical/High/Medium issue. Unproven core
collector and privileged attach-controller scenarios were not accepted as
additional findings. The gate is not clear. The same implementation agent must
follow [the Round 1 repair plan](08-round1-repair-plan.md), reach FINAL again,
and undergo all three lenses on a newly frozen delta. Step 9 remains inactive.

## Main advanced during Step 8 repair

A read-only remote-head check found Bao main unchanged and UI main advanced
by four chat/editor fixes to ac38edf7e57dd568c81b66e16436c982e9474dc2.
At an acknowledged implementation boundary, root backed up all 105 port paths
and fast-forwarded only the isolated UI feature branch. All 13 upstream paths
were disjoint, and every port path and content hash remained identical. No
new commit was created; original checkout branches and files were untouched.
Evidence is in `step8-new-main-{before,after,verification}.json` and the saved
upstream patch under the task scratch evidence directory.

The focused repaired native selection suite passed 13 tests before the
fast-forward. Final validation now includes a new full UI run on the newer base,
and Round 2 reviews will use ac38edf7e57dd568c81b66e16436c982e9474dc2.
The old full-suite and Round 1 evidence retain their original base identity.

## Step 8 repair FINAL and Round 2 activation

The same Astra max implementer reached actual FINAL on UI base ac38edf7 after
repairing both findings. Full dotted IDs now cross real registered Tauri IPC
and retain their suffix in synthetic modify/query requests. Lifecycle intent
is captured before authorization and before queue submission; deterministic
one-worker tests cover first acquisition across close, reload and Any-session
reset. Existing native ownership of active writes remains intact.

All final gates passed: 874 native tests (five ignored live tests and four
real-keychain exclusions), 31,360 UI tests across 2,033 files, both Clippy modes,
Rust formatting, typecheck, all 18 selected lint:push tasks and exact 39-file
generated IPC parity. Seven targeted guard mutations failed by assertions and
were restored. The repair changes five port paths: native coordinator, tests
and feature spec. It changes no renderer or Bao product code.

Root read the report and new real-bridge/controlled-queue tests. Complete
inventory and exact logs are linked by
`/private/tmp/anybao-oauth-main-evidence/step8-r1-repair-report.md`.
Root now freezes the complete paired delta and resumes the same three Astra
xhigh reviewers. This is validation completion, not yet a cleared review gate.
Step 9 remains inactive and live acceptance remains user-owned.

## Step 8 gate cleared; Step 9 active

All three same Astra xhigh reviewers reached FINAL with H1 and M1 closed and
no new or unresolved Critical/High/Medium finding. Root read all reports and
the source/test closure evidence, then recomputed both complete inventories:
UI 105 paths (0c2be087af9296c741f20c88281c0f0537c726ca4149c169a713163f31f990cb)
and Bao 34 paths (b5b73b956908888697cb03ed820b3516ca255cb8f60eaab50bee9544aa63fa60)
remained identical through review. The supplied native test executable also
remained unchanged. Bao product source remains the approved Step 7 tree.

Independent security checks passed 39 native and 75 UI tests; lifecycle passed
27 native and 49 UI tests; integration passed both native bridge tests and
227 UI tests. These overlapping independent runs are not a unique-test total.
All lenses checked exact generated IPC parity. Source review and synthetic
checks establish closure; no packaged, visual or live acceptance is implied.

Root's final adjudication is in the task scratch evidence directory at
`step8-round2-adjudication.md`. Step 9 now follows
[the validation and handoff plan](09-validation-handoff-plan.md): a narrow
local development build, isolated backend/source deployment and exact launch
command, then the same implementation/three-lens repair loop. The user owns
interactive testing; original profiles/checkouts remain outside this work.

## Step 9 implementation and handoff verification

The implementer delivered a development-only prepare/verify/launch helper,
source-readback module, 14 focused tests, gate wiring and a concrete runbook.
The complete change inventory is UI 110 paths and Bao 35 paths, including
untracked additions. Against the Step 8 freeze, only five UI helper/runbook
paths and the two root-owned Bao plan/progress documents differ. Previously
reviewed UI and Bao product bytes remain identical.

The ad-hoc macOS arm64 debug app and deploy CLI embed the paired guest kernel.
Full bundle hashes, codesign checks and signature-normalized SDK comparison
establish the artifact inputs. The owned synthetic managed backend has 27
agent and 23 connector programs, with exact code/manifest readback and pinned
metadata. Four backend boots retained the native account/device identity.
The first app launch remains unconsumed; no GUI, real custody, CLI discovery,
OAuth or inference was run by agents.

All 36 selected lint:push tasks and the 14 helper tests passed. Eleven scratch
mutations were caught by assertions. The canonical app/kernel/deploy builds
passed with no tracked generation drift. Earlier failed environment/probe
attempts are retained in the evidence rather than silently omitted.

Root read the implementation report, full helper/tests, runbook and corrected
inventories. The concrete handoff and complete paired changes now enter the
three independent Astra xhigh review lenses. Step 9 is not yet cleared.
Evidence: `/private/tmp/anybao-oauth-main-evidence/step9-final-report.md`.

## Step 9 Round 1 review and narrow correction

All three Astra xhigh lenses reached FINAL with zero C/H/M. Root read their
reports and independently verified that the complete UI110/Bao35 freezes did
not change during review. Independent tests covered fake-child isolation,
actual controlled child shutdown, explicit restart, complete source/artifact
identity and supplied guest-source readback. No user fixture was launched.

One Low runbook navigation error is confirmed: Settings labels differ from
Welcome. The same implementer now corrects that literal path only, followed
by focused three-lens closure. No product change or rebuild is required.

The same implementer reached repair FINAL after changing only the runbook's
two Settings-navigation lines. Root read the repair report and exact diff.
Literal renderer-label checks, targeted formatting and diff checks passed;
app/SDK/deploy hashes remain unchanged. All earlier product/helper gates are
retained. The final three-lens review is limited to this documentation closure
and verification of unchanged source/artifact identities.

## Step 9 complete — user handoff ready

All three same Astra xhigh reviewers reached actual Round2 FINAL, closing L1
with no new or unresolved Critical/High/Medium findings. Root read all reports,
confirmed the literal Settings sequence, and recomputed both complete source
freezes after review: UI110/Bao35 remained byte-identical. Full app bundle and
executable hashes still match the prepared artifact; first launch remains
unconsumed and no run lock is present. This final ledger update changes only
this root-owned document, which is excluded from launch/build input identity.

The paired branches remain uncommitted on UI ac38edf7 and Bao fb1de0f. Main's
CLI providers coexist with independent fresh ChatGPT OAuth. The reviewed
macOS arm64 local handoff is ready at the runbook's exact command. Live GUI,
OAuth, OS custody, installed CLI discovery and inference checks remain owned
by the user. Root's final evidence is step9-round2-adjudication.md and
step9-round2-root-verification.json in the task scratch evidence directory.

## Step 10 active — Provider UI correction

The user ran the Step 9 app and supplied screenshots of a connected ChatGPT
provider. They request moving the separate subscription section into Provider
and reducing the expanded control clutter. Root verified the entire Step 9
delivery inventory is unchanged before activating 10-provider-ui-plan.md.
The user's existing test profile now contains a real connection and remains
outside automated access. Follow the same Astra max implementation and three
Astra xhigh review loop; a later scoped build handoff will preserve that state.

## Step 10 implementation FINAL

The Astra max implementer reached actual FINAL with the compact Provider flow,
one model dropdown and Save/Cancel, secondary account management and contextual
recovery. Root read the final report and inspected the controller, Provider
save/draft paths, account panel, model field and readiness view. The 20-path UI
delta is frozen within a complete 119-path branch inventory. Native/launcher
sources and all 41 files of the previous app bundle are unchanged.

Final checks pass: 210 affected/shared tests across 17 files, typecheck, typed
lint, all 36 selected lint tasks and fresh frontend/Storybook static builds.
Earlier lint failures are retained with their fixes in the implementation
report. No app, live profile, CLI discovery, OAuth or inference was exercised.
Three independent Astra xhigh lenses now review the frozen source; Step 11
and a new runnable artifact remain pending.

## Step 10 complete — Step 11 active

All three Astra xhigh reviewers reached actual FINAL with no C/H/M or Low
finding. Root read all reports and independently re-froze complete UI119/Bao36
inventories: byte-identical before/after review. Independent serial focused
runs passed 30 product, 69 security/integration and 38 lifecycle tests. There
is no finding to repair. Adjudication and exact hashes are recorded under
`/private/tmp/anybao-oauth-main-evidence/step10-r1-*`.

Activate [Step 11](11-preview-update-plan.md): build a separate preview without
replacing the prior Any.app, and prepare an explicit user-invoked metadata
update preserving the current profile and OAuth namespace. The user's live
fixture remains outside all agent access; helper checks use synthetic fixtures.
Only after its own implementation and three-lens gate will the runnable update
be delivered. No visual/live acceptance is claimed by the source gate.

## Step 11 complete — compact Provider preview ready

The Astra max implementation reached actual FINAL with four tooling/runbook
changes, 64 passing helper tests, focused typed lint and all 36 selected lint
tasks. Canonical packaging produced a separate `Any Provider Preview.app`;
all 41 files of the previous Any.app remain unchanged. Official/staged/bundled
SDK content, guest kernel, anyrt and every Step 10 product source are preserved.
Production update/no-op checks ran only on a fresh fake root and ephemeral port.

All three Astra xhigh reviewers then reached actual FINAL with no C/H/M or Low
finding. Independent security tests passed 41 cases and lifecycle passed 13;
the artifact lens independently verified signature, architecture, complete old/
new bundles, namespace derivation and exact user commands. Root read all reports
and independently re-froze UI120/Bao37 after review: identical to the review
baseline. An apparent SDK discrepancy was disproved as two distinct file paths;
its rejection and all gate evidence are retained under
`/private/tmp/anybao-oauth-main-evidence/step11-r1-*`.

The separate macOS arm64 preview is ready for user-owned acceptance using the
paired UI runbook's Cmd+Q/wait, explicit metadata update, then separate
`--resume-fixture` commands. Recipe SHA-256 is
`d344b0d66f084db515ed046444956ae3e8801f8eef4fb383dd92624664b70e96`.
This final ledger update is the only post-review source change and is excluded
from the launch/build input identity. Both branches remain uncommitted at their
pinned main bases. No live-root inspection/update/launch, app execution, real
custody/CLI/OAuth/inference, installed-app replacement or release occurred.

## Step 12 active — recover denied access and deleted wrapping keys

The user reports the same custody error after deleting Keychain entries and
restarting. Source confirms suppressed dialogs without an interactive recovery
action, and a disconnect path which itself requires decrypting the old record.
The actual live OS error is unobserved. Root verified the entire Step 11 delivery
inventory before activating [Step 12](12-custody-recovery-plan.md). Fix and review
use synthetic stores only; the user's profile, OS custody and existing bundles
remain untouched. A new runnable handoff is a separate subsequent step.

The user then requested a dev-start command and manual testing. Browser
`pnpm go` was supplied with its native-OAuth limitation. The Step 12 plan now
includes the previously planned Step 13 artifact handoff under the same
implementer, followed by one complete three-lens source/artifact review gate.
This avoids a second packaging-only review cycle while preserving the user's
zero-C/H/M requirement. No runnable recovery build or live acceptance is claimed.

## Step 12 implementation FINAL — source and artifact review

The same Astra max implementer reached actual FINAL with explicit cancellable
macOS access authorization, a confirmed scoped reset for missing keys or corrupt
current records, and compact shared Provider recovery actions. A distinct
`Any Recovery Preview.app` is built. Both earlier complete bundles and the
guest/kernel/SDK/deploy-CLI pins remain unchanged. The v1 updater stays strict;
the explicit v2 recipe accepts the two exact known earlier endpoints while
preserving profile, custody namespace, deployment and all non-build metadata.

Source gates pass: core 455, focused shell custody/deadline/shutdown 9/1/8,
safe native 890, affected UI 273 across 19 files, updater 110, both core/native
Clippy modes, typecheck, IPC parity and all 36 selected lint tasks. Nine removed
guards failed assertions, then exact restoration was verified. Four production
update/no-op invocations passed on fresh fake roots only. The damaged partial
broad-shell log is excluded; the source report identifies the accepted evidence.

Root read the source/artifact reports, runbook and relevant implementation,
verified gate results, independently rehashed all three complete 41-file app
bundles, and matched recipe source identities to the built inputs. Recipe SHA-256
is `14d9a21c3e2f838a0c255d5ace6b5b5f7a086851cd68322b05f7710484416b70`.
The complete paired changes and exact Step 12 deltas now enter the three
independent Astra xhigh review lenses. The user-owned live profile remains
untouched; no app, real custody, OAuth, inference or actual OS dialog was tested.
The runnable handoff is not cleared until all three reviews and adjudication.

## Step 12 Round 1 — one updater durability correction

All three Astra xhigh reviewers reached actual FINAL with one shared Medium
finding and no other C/H/M/Low finding. Root confirmed M1: after rename succeeds
but directory sync fails, a validated no-op retry can report success without
finishing the durability barrier. The synthetic production-code repro confirms
the missing retry sync; actual crash or credential loss is not claimed.

Root verified complete UI124/Bao40, recipe, all three bundles and pinned artifacts
remain unchanged after review. The same implementer will repair M1 with focused
red/green regressions, refresh immutable handoff evidence and exact commands,
then all three lenses review again. An unchanged app bundle may be retained only
with explicit proof that changes are limited to unbundled updater/test inputs;
the new handoff source identity must not be misrepresented as the original
build-time broad inventory. The user's live profile remains outside all access.

The same implementer reached repair FINAL. Validated no-op now completes the
directory sync under the existing lock and propagates repeated failure without
rewriting the manifest. New v1/v2 open/sync regressions failed before the fix;
all 115 updater tests, 36 lint tasks and four fake production CLI invocations
passed afterward. A removed retry-sync guard caused the expected assertion
failures and was restored exactly.

The Recovery app is retained byte-for-byte. Only the external updater and its
test differ among selected inputs; all 6192 other UI and 311 Bao inputs, three
complete bundles and all pins are unchanged. New handoff source identity
`6e2d8ee707d8fa34a5295a069b49b3951dc240f4fab7d07a0152ae0e38ff1043`
is explicitly distinguished from the original broad build inventory. The new
immutable recipe `step12-r2-impl-native-update-recipe.json` has SHA-256
`f65f67d203ba5411ed97f56ee20d47fb888c08708c044672d8d1d67a2244edc0`.
Root read the repair/evidence and now freezes it for all three repeat reviews.

## Step 12 complete — recovery preview ready for manual testing

All three same Astra xhigh reviewers reached actual Round 2 FINAL, closing M1
with no new or unresolved C/H/M/Low finding. Root read all reports and confirmed
the original reproducer now passes. Independent focused fault tests, the full
115-test updater suite and four fresh fake-root production CLI invocations pass.
Root re-froze complete UI124/Bao40 and independently checked all three full app
bundles, pins, current source identity and exact new recipe after review: no drift.

The retained `Any Recovery Preview.app` and new Round 2 recipe are ready for the
user's Cmd+Q/wait, metadata update and separate `launch --resume-fixture` sequence
in the paired UI runbook. Explicit system access, confirmed reset if the wrapping
key is missing, fresh sign-in, inference and restart remain user-owned live
acceptance. No protected live-root, real custody/credential or app access occurred.
This final ledger edit alone follows review and is excluded from build inputs.
Evidence: `step12-r2-adjudication.md`, `step12-post-r2-r2-root-verification.json`
and the delivery freeze under the task scratch evidence directory. Both paired
branches remain uncommitted; original checkouts and previous bundles are intact.

## Step 13 active — default model for fresh setup

The user's screenshots show a connected ChatGPT account with an empty model
selection. They request a default, suggesting GPT-6.1 Sol. Root confirmed the
shared draft currently falls back to an empty string and verified complete
Step12 delivery inventories before activating [Step 13](13-default-model-plan.md).
The change will preselect `gpt-6.1-sol` only for a fresh draft, preserve stored
and edited choices, and retain explicit Save. Source and runnable handoff receive
the same Astra max implementation and three Astra xhigh review loop. No live
profile or credential access is authorized or required for the default.

The Astra max implementer reached actual Step13 FINAL. An explicit shared
`gpt-6.1-sol` constant replaces only the untouched draft's nullish fallback;
saved/custom/edited choices and all Save/Apply gates remain. Expected-red
Settings coverage and 163 focused tests, typecheck, typed lint, required lint
and static story/doc checks pass. No native or Bao product source changed.

A distinct `Any Model Preview.app` is built; all three previous bundles and
pins are unchanged. The strict renderer-only v1 recipe uses the exact Step12
Round2 destination and fixed fields; production update/no-op passed on a fresh
fake root. Recipe SHA-256 is
`1331a99242eb0d0eaba5dc681f8d61d5656b8d01fe3a0e8c18c397e79bad934d`.
Root read implementation/build/artifact evidence and now freezes complete
sources/delta for three independent Astra xhigh reviews. User-owned live testing
remains pending; no live profile, credential or app execution occurred.

## Step13 complete — fresh default model preview ready

All three Astra xhigh reviewers reached actual FINAL with zero C/H/M/Low
findings; root read and accepted their reports. Independent product coverage
passed 43 tests, lifecycle's isolated proof passed 51 assertions with four
in-memory mutants rejected, and security independently verified the complete
artifact/recipe and production update/no-op on a fresh fake root.

Root re-froze complete UI124/Bao41 after review: unchanged. All four bundles,
pins, current source ordering, strict v1 prior endpoint/fixed metadata and the
new recipe match. Fresh Settings/welcome now suggest `gpt-6.1-sol`; saved and
edited choices remain exact, and Save/Apply remains explicit. The distinct
Model preview is ready at the runbook's user-only update/resume commands.
Manual appearance and live-provider testing remain with the user. This final
ledger edit is the only post-review change and is excluded from build inputs.
Evidence is `step13-r1-adjudication.md`, `step13-post-r1-root-verification.json`
and the delivery freeze in the task evidence directory. No live-state access,
app execution, original checkout modification, commit, push or release occurred.

## Step 14 implementation — documentation closeout

Provider navigation redesign is deferred to separate work (owner decision,
2026-11-05), as recorded in the owning UI spec.
**On this computer** and **ChatGPT subscription** controls obscure the hierarchy
by appearing nested. Both alternatives remain open: put local CLI choices at
the provider selector's level, or group Codex within OpenAI and Claude Code
within Claude. The future design must also clarify ChatGPT subscription
sign-in placement. No alternative is selected or implemented.

The latest macOS arm64 Model preview's appearance and operation received limited
manual acceptance (owner report, 2026-11-05), without claiming
scenario-by-scenario OS custody/dialog, recovery, login/inference/restart,
Light/Dark, other-platform or release acceptance. Existing source gates and
reviews remain evidence for unchanged product code; this step changes only the
owning spec and this ledger and runs documentation checks only.

Documentation review and separate delivery snapshots remain pending. Commits,
pushes, two linked PRs and coordination through any-agents are authorized
(owner decision, 2026-11-05); root owns those subsequent actions. The delivery presents new
ChatGPT subscription support that does not require installing Codex or Claude
Code. Device-code login allows approval on another device; no standalone
headless setup command is claimed.

The preview worktrees' HEAD, branch and index remain untouched so their launch
identities stay pinned. This documentation step performs no code change, rebuild,
live-state or credential access, or app/process operation.
