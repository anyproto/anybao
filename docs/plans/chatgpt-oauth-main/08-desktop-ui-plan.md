# Step 8 — Current desktop and UI integration

Activated 2026-11-03 after Step 7 cleared three independent review lenses with
zero Critical/High/Medium findings. One GPT-6 Astra max implementer, then three
GPT-6 Astra xhigh reviewers after actual FINAL and a frozen paired delta.
Root adjudicates and resumes the same implementer until no C/H/M issue remains.
Step 9 remains pending this gate.

## Workspace and baseline

Product edits: paired any-ui worktree
/Users/roma/anytype/anybao/.worktrees/chatgpt-oauth-main/any-ui, branch
feat/chatgpt-oauth-main. Initial base was
52683f5feffefa74435cfc9c02483937e40bb281. During Round 1 repair, current main
advanced to ac38edf7e57dd568c81b66e16436c982e9474dc2; root fast-forwarded
this isolated worktree after confirming all 13 upstream paths were disjoint.
Every port file hash remained unchanged. Round 2 and final validation use this
new base; earlier baseline and Round 1 evidence describe the initial base.
Paired Bao is the reviewed Step 7 implementation. Report needed core changes
before editing them; keep an explicit cross-repository delta for review.

Reference old OAuth UI only at
/Users/roma/anytype/any-ui/.worktrees/chatgpt-oauth. Do not copy its broad diff;
current main has substantial new native lifecycle, observability, multi-window,
model, onboarding and sync behavior. Original checkouts remain untouched.

The current UI typecheck passed. Full unchanged-main baseline had 31,179 passes
and three table/editor failures; all three files then passed together (249
tests) with one worker and no heavy concurrent build. No source fix was made;
load or order remains a hypothesis. No Figma console tool is available and
interactive UI testing is assigned to the user, not an implicit agent action.

## Required reading and existing contracts

Read AGENTS.md in manageable complete chunks for applicable sections, plus the
frontend delivery and desktop-shell playbooks. Owner specs include local AI
proxy/model settings/auto-connect (2026-10-13 and 2026-10-19), setup loader
(2026-10-22), first welcome message (2026-10-28), multiple windows (2026-10-02),
credential repair/grouping (2026-10-03), managed-auth lifecycle (2026-09-10),
PR-217/218 credentials, PR-219 model settings and PR-106 observability. Changes
under src/lib/sync require PR-072 and the sync-store playbook. Write a current
dated spec with last-verified frontmatter; preserve existing component/design
conventions rather than restoring the old welcome layout.

## Native integration

1. Port the dedicated encrypted grant vault, scoped identity and bounded
   noninteractive OS wrapping-key custody. Access and refresh stay local;
   generic secrets, webview, sync, traces and exports receive no token. Keep
   separate from recovery-phrase custody. Preserve cancellation, refresh
   ambiguity, logout/account switching and cross-process fencing.
2. Share one native ChatGptAuth with embedded Bao and current Any session
   control. Keep current startup, stop/restart, exit/update and report behavior.
   Carry the secret-memory restriction: raw process dumps cannot contain OAuth
   material. Preserve sanitized event/crash/session reporting and current SDK
   behavior; do not transplant obsolete Sentry code.
3. Authorize real registered workspace webviews and their exact trusted origins.
   Current main has main plus workspace-* windows over one process. Preserve
   one shared native session and window-appropriate presentation/events;
   closing one window must not cancel another window's newer login.
4. Follow current generated IPC typing: ts-rs-derived native DTOs, generated
   TypeScript, schemas annotated against generated types, command/event unions
   and the IPC gate. Update COMPATIBILITY.md for per-OS implementations, and
   distinguish source support from actual OS validation.

## Product integration beside CLI providers

1. Keep the existing any-ai proxy, CLI admission rules, catalogues, port repair,
   API-key choices and auto-connect behavior. Add a clearly separate fresh
   ChatGPT OAuth option in current onboarding, Settings Model and Credentials.
   OAuth never imports/falls back to CLI credentials. Browser/remote consumers
   must render stored OAuth choices and readiness honestly; native sign-in is
   desktop-only.
2. Preserve current first-run loader and seeding guard. CLI auto-connect must
   not overwrite an explicit OAuth pick, pending login/apply, or stored OAuth
   target. Switching back to a CLI or API preset must handle target state and
   search ownership coherently, without leaving the old OAuth target blocking
   readiness or deleting custom search/judge settings.
3. Integrate the final Step 7 atomic fresh-apply, read-only retry and scoped
   cancel contract. A fresh selection uses a new apply ID; an ambiguous old
   batch must not be silently replayed after a later selection/cancellation.
   Observe exact target/readback and responder scope/generation, not merely a
   native Connected flag. Preserve explicit search intent and own JSON keys
   such as __proto__ using shared Rust/Python/TS canonical fixtures.
4. Provide explicit model selection for fresh OAuth, independent of actual CLI
   discovery. Preserve a stored model verbatim. The old live check used
   gpt-6-sol; public names checked 2026-11-03 are in model-reference.md in the
   task scratch directory. A bounded custom model-ID field avoids claiming a
   static list is an account entitlement response. Do not claim new live
   compatibility from documentation alone.
5. Preserve the first Bao welcome-message behavior after successful setup.
   OAuth has no synchronized credential-token row: do not fabricate one or
   falsely announce that a secret was written. Use an honest existing message
   or explicit typed model-connected signal with corresponding runtime handling.
6. Cover connected/disconnected/connecting/reauth/unavailable, cancellation,
   account/device/window switch, stale events, partial/rejected/ambiguous apply,
   recovery, and moving between OAuth, CLI and API selections. Keep user copy
   about actions and subscription use, without implementation details.

## Validation and handoff boundary

Source /private/tmp/anybao-oauth-main-env.sh. The task-local pinned pnpm binary
requires escalated execution in this sandbox; invoke its full path to avoid
falling back to the incompatible system version. Rust 1.99.0, Node 26.8.1,
pnpm 12.11.2, Python 3.14.7. SDK v0.3.4 (current UI CI default) is already
staged in this worktree with manifest verification and ad-hoc signing; no app
has been launched. Native Cargo commands run from src-tauri so its macOS
deployment target config is used. Reconcile the anyrt path-dependency lock
without a broad unrelated upgrade.

Run focused native fake-custody/session/window tests, focused UI tests with
restored stubs (no vi.mock/resetModules), typecheck, affected typed lint/IPC,
canonical native checks and the required full UI suite/lint gates serially.
New untracked files must actually be covered by diff-aware gates (intent-to-add
is reversible; no commit). Inspect native test safety before broad execution:
skip real keychain probe_is_infallible, set_get_roundtrip,
services_are_isolated, list_and_delete and all ignored live CLI/dev-server tests.
Do not infer visual/live acceptance from mocks or source checks.

No real profiles, auth files, keychain items, wallets, CLI discovery, GUI/main
app/browser, live OAuth/inference, shared deployments, commits/push/releases or
installed-app replacement. Scratch fixtures/fake stores/loopback servers only.
Step 9 owns the exact runnable artifact and isolated guest source deployment;
a normal native rebuild still uses shared overlays unless explicitly redirected.

## Source reading details resolved during Step 7 review

Current owner specs (exact paths under docs/specs):
- 2026-10-13-local-ai-proxy.md
- 2026-10-13-local-ai-model-settings.md
- 2026-10-19-local-ai-auto-connect.md
- 2026-10-22-bao-setup-loader.md
- 2026-10-28-bao-first-message.md
- 2026-10-02-multiple-windows.md
- 2026-10-03-chat-credential-repair.md
- 2026-10-03-credentials-grouped-by-status.md
- 2026-09-10-managed-auth-login-logout.md
- PR-217-credentials-in-settings.md
- PR-218-credentials-screens.md
- PR-219-model-in-settings.md
- PR-106-tauri-sentry-observability.md
- PR-072-syncstore-rewrite.md for sync edits.

Old OAuth UI source includes dedicated src-tauri/src/chatgpt modules, platform
chatgpt DTO/schema/controller modules, model apply/recovery/readiness helpers,
account-bus protocol, credential/setup components and tests. Reimplement around
current files; old generated/spec/coverage diffs are not authoritative.

Root inspected current packages/api-core/src/client.ts and netFetch.ts:
apiFetch/performApiFetch and netFetch issue one request, with no app retry loop.
Desktop bootstrap installs platformFetch, exported from src/lib/platform/net.ts;
its plugin/reqwest path still needs verification before claiming single delivery.
The fresh batch is never automatically resent after ambiguity; a read-only
verify operation may be retried. Preserve readback and explicit fresh Apply.

Current auto-connect lives in src/components/chats/useLocalAutoConnect.ts.
Its in-flight map is per JS context, while the app supports multiple workspace
windows. Check explicit OAuth intent/pending login and delayed auto-connect in
that real setting; don't assume a module global coordinates every window.
Current first-welcome announcement is in announceModelConnected.ts and currently
expects a secrets anchor/ref written by the connect; OAuth needs an honest
message/typed signal rather than a fabricated synchronized credential row.

Exact packaged backend: official any v0.3.4, built from
6a678a4ee411100c56328ad92ecd66b897e7a5f8, embeds any-sync-sdk v0.6.4.
Read /private/tmp/anybao-oauth-main-evidence/packaged-backend.md and
/private/tmp/anybao-step7-r1-sdk-and-transport-evidence.md. Do not confuse the
older sibling any checkout (SDK v0.4.3) with this staged binary. Exact public
source was fetched into scratch and can be read without executing a server:
/private/tmp/anybao-step7-any-v0.3.4/github.com/anyproto/any@v0.3.4
/private/tmp/anybao-step7-sdk-v0.6.4/github.com/anyproto/any-sync-sdk@v0.6.4
