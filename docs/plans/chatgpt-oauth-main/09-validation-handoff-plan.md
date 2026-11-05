# Step 9 — Paired validation and a runnable local handoff

Activated 2026-11-03 after Step 8 Round 2 passed all three Astra xhigh lenses
with zero Critical/High/Medium findings. Root read all FINAL reports and
independently verified unchanged paired inventories and native executable.
One GPT-6 Astra max implementer owns this step, then three independent GPT-6
Astra xhigh lenses. Root confirms/rejects every finding and resumes the same
implementer until no Critical/High/Medium issue remains. No GUI or live authentication/inference is run
by agents; the user owns interactive acceptance.

## Purpose

Deliver a concrete local build and an exact launch command for the paired
feat/chatgpt-oauth-main worktrees. Preserve original checkouts and installed
applications. No commits, pushes, shared overlay deploys, release or upload.
Verify the final source/build identities and don't call older live acceptance
proof of this port.

## Critical deployment boundary

The desktop embeds anyrt but gets its Python programs from configured overlay
spaces. The default src-tauri/src/agent.rs joins published stable/nightly spaces.
A rebuilt desktop alone therefore cannot validate this feature. Use the
ANYBAO_TOML override (read verbatim) with isolated local overlay spaces containing
this worktree's repos/_agent and repos/_connectors, and read back program source
or hashes to prove the exact changed guest code is what the app will resolve.
Do not publish anything to stable/nightly/shared repo accounts.

ANY_DATA_DIR creates an independent app instance, backend data directory,
keychain namespace and app-local files. Use a fresh task-owned data directory,
never an existing user's account/profile, and an explicit separate
ANY_DESKTOP_PORT (the debug default is 31350..31359). A custom config should use
agent.control_port = 0 to avoid collision. The backend address in ANYBAO_TOML
is overridden by the desktop's actual local backend, so do not rely on that
field to attach to a different database.

A scratch local server may be created solely for setup and synthetic checks.
Use the officially staged SDK binary, not an accidental sibling rebuild. Pass
an explicit config file; do not allow ~/.config/any/config.yaml fallback. The
current desktop selects <ANY_DATA_DIR>/config.yaml if present and passes it via
--config. Establish local-only behavior before account creation/deployment:
no shared network, LAN discovery, global P2P relays, push or external access
service. scripts/dev/nodeconf-local.yml is a reference but its placeholder
hosts should not be mistaken for an enforced network isolation boundary;
inspect the actual SDK behavior/config. Explicitly disable p2p.enabled and
p2p.global.enabled and avoid ambient environment overrides. Use loopback-only
placeholder addresses where the SDK requires nodes. Do not read or print real
wallets, recovery phrases, credentials, CLI auth stores or keychain entries.
Fresh synthetic account setup is permitted; avoid printing even its secrets.

Do not run the normal scripts/dev/dev.sh: it can open Chrome, read default
seeds/profiles, reclaim unrelated ports or build into original sibling repos.
Do not run ordinary pnpm tauri dev: its beforeDevCommand rebuilds the sidecar.
Prepare a standalone ad-hoc debug app/build or an explicit supported build
configuration that uses the verified staged SDK without launching it. Keep
Apple signing/notarization/release credentials unset and do not replace an
installed app. A launcher should refuse missing/mismatched artifacts or occupied
ports rather than killing unrelated processes or falling back to a default
profile/shared overlay. A launch may perform task-owned local setup on first
run only when that behavior is documented and bounded.

## Validation

Use the task environment /private/tmp/anybao-oauth-main-env.sh and pinned tools.
The task-local pnpm executable requires sandbox escalation. Coordinate heavy
builds and full tests serially. Reuse completed source checks unless the handoff
adds changes or uncovers a gap. Ensure diff-aware lint includes new files.
Inspect actual native tests and skip OS keychain probes and all ignored live
CLI/dev harness tests. Reconcile and record full-suite baseline flakes honestly.

Run safe launcher/config tests and syntax/static checks; verify the artifact,
source hashes, staged SDK identity and deployed program readback. Avoid claiming
GUI rendering, OS custody on other platforms, OAuth login or provider entitlement
from synthetic tests. Root verifies original checkout preservation and freezes
both repo deltas before the three reviews.

Final handoff should include the exact command, worktree/branch paths, current
main base SHAs, passed checks/reviews, and the user's manual checklist: distinct
CLI/API/OAuth choices; fresh sign-in; model choice; ordinary reply/tool turn;
cancel/reconnect; app restart; switch among providers. No claim that main's CLI
path copies tokens into Bao—the current architecture drives actual CLI sessions
through the any-ai proxy, while fresh OAuth uses direct Responses.

## Additional exact-source checks for the implementer

The public packaged Any v0.3.4 source is available at
/private/tmp/anybao-step7-any-v0.3.4/github.com/anyproto/any@v0.3.4.
Its internal/config/load.go layers file, then environment, then flags. Ambient
ANY_NETWORK_NODECONF_PATH, ANY_ACCOUNT, ANY_WALLET_PATH, push/global-P2P/index
variables can defeat an otherwise isolated file; construct a bounded child
environment without inspecting or printing their values. Default indexing is
on with an auto embedder. Scratch validation must disable indexing/local model
inference as appropriate so startup cannot trigger a cloud call or model fetch.
Use explicit network and P2P/push settings, not the bare default config.

The desktop debug-only attach mode is ANY_DESKTOP_ATTACH_ADDR=host:port with
optional ANY_DESKTOP_ATTACH_CONTROL_TOKEN. It never owns/stops the attached
server; auth-control commands refuse without a valid managed control token.
A malformed attach value falls back to spawning the ordinary sidecar, so a
handoff launcher must validate before launch and not rely on the fallback.
Attaching a standalone server would limit login/logout checks; prefer preserving
the managed lifecycle if using attach. These are options to evaluate, not an
instruction to launch a GUI or read a real profile.

Without an agent overlay, Bao's alias_map binds `agent` to the working space
(runtime/src/serve.rs:1288). Single-space local deployment is therefore another
possible explicit override, but verify connector aliases/inventory and startup
ordering before treating it as a full handoff. Do not merely change the desktop
binary while it still joins the published agent overlay.

Canonical non-launching debug packaging is `pnpm tauri:debug --bundles app`:
scripts/dev/tauri-debug.js calls Tauri build --debug with tauri.debug.conf.json
(ad-hoc signing, updater artifacts disabled). Its beforeBuild hook rebuilds the
paired kernel and frontend, not the SDK; beforeBundle signs only staged nested
code. Unlike tauri dev, this route does not call tauri:sidecar. Validate exact
flags/environment before using it, explicitly clear ANY_DEV_SIGNING_IDENTITY
and Apple signing/notarization/release credentials, and keep SDK v0.3.4 staged.
Source-map uploads are guarded in vite.config.ts; keep the handoff out of CI/
release upload modes and do not add upload steps. No GUI is launched by build.

The deploy CLI accepts `--config-file`, `--addr` and an explicit `--target`.
Use the task-owned config directory: `Config::load` also opportunistically
reads `.connectors.env` beside that file, even for a deploy command. Do not let a
missing explicit config select the checkout's/default config or secrets file.
`deploy` writes programs, skills and README only; it does not start `serve`.
Prove deployment by reading the stored `main.code` source (and metadata where
needed), not only accepting the CLI's created/unchanged summary. Its program
fingerprint includes summary, tool flag, category and manifest as well as code.

Account bootstrap must match the launch mode. Packaged `--mode managed` never
opens an on-disk standalone wallet and starts unauthorized; merely predeploying
to a standalone scratch account does not make that account active in the
desktop. The packaged server can mint a fresh `CONTROL_TOKEN` at startup and
accept a synthetic-account POST `/v1/auth`; its response includes the fresh
mnemonic, which must never be printed. A launcher-owned managed backend plus
the debug attach address/control token is one option to evaluate. If used,
the launcher must own and reap only that backend, persist any synthetic test
identity privately, and preserve explicit logout rather than silently logging
back in. Do not silently start a different account whose local overlays are
missing. Read the exact packaged server behavior and choose a documented,
bounded bootstrap; this note does not authorize launching a GUI during setup.

Set `ANYBAO_DIR` explicitly to the paired Bao worktree for the debug build.
`scripts/dev/kernel.sh` gives that environment override precedence and its
sibling helper can otherwise find the primary checkout. Also keep `CI` unset:
the kernel hook skips rebuilding an existing kernel in CI. Verify the embedded
kernel input identity; do not let an ambient override build an original repo.

`sidecar::start_agent_when_authorized` runs in debug attach mode too and binds
native ChatGPT custody before starting embedded Bao. A prepared managed test
account therefore uses the real host binding path. Do not launch a second
`anyrt serve` beside the app. A launcher-owned backend must remain alive for
the app lifetime and stop only its owned child on exit. Preserve the distinction
between app restart, backend restart (new control token/boot) and Any logout;
do not silently repair an explicit logout by repeatedly restoring the fixture.

The packaged placeholder nodeconf still contains external fake DNS names.
Replace all node addresses with loopback in the task-owned config; merely
selecting that placeholder is not sufficient to keep scratch probes local.
The packaged managed auth handler never enumerates wallet files and refuses
accountId selection. A synthetic fixture uses a newly generated or task-private
synthetic mnemonic only, never a user's phrase. Never print the raw auth
response because it contains that mnemonic. Keep setup failure logs scrubbed.

Current paired bases after the Step 8 refresh: any-ui ac38edf7e57dd568c81b66e16436c982e9474dc2; anybao fb1de0f0732e296693116b1b0e80f64d7485ad1d. Earlier UI Round 1 evidence used 52683f5 and must not be described as validation of the later base.

Inspect the desktop artifact with file/hash/otool/codesign metadata; do not run
its executable even with a presumed --version/--help flag, since a desktop
binary may initialize the app instead. The nonlaunching setup/verify path must
be separate from the user-owned launch path. Tests for the launcher can inject
fake children or stop after scratch-backend/source verification, with no actual
CLI discovery, webview, keychain or inference side effects.

Preserve the derived working-space contract. Current UI resolves the `bao`
entry from GET /v1/spaces/derived and never identifies the agent space by its
display name (docs/derived-bao-space.md, src/lib/api/spaces.ts). Runtime
ensure_space resolves `agent.space = "bao"` through that same registry and
materializes it idempotently with POST /v1/spaces/derived/bao. A generic newly
created space merely named "bao" will not appear as the agent space in the UI.
If the setup deploys directly to the working space, use the registry-derived
ID. Separate local source overlays may be ordinary scratch spaces, while the
working space must remain the account's derived Bao space. Also preserve
bao/v1 and general-chat/v1 bundle roots/children rather than guessing object
names. This is source preparation evidence, not an instruction to start serve
or the GUI during automated setup.

Keep `[agent] space = "bao"` in the app override: `ensure_space` takes a
registry/display name and does not treat an arbitrary ID as a lookup key.
Use explicit space IDs for deploy `--target` and `[overlays]` entries, which
have their own ID contracts. Do not accidentally configure an ID as the
working-space name and cause a second ordinary space to be created.

For the local packaging child, force `SIGNING_MODE=adhoc` (the nested signing
hook otherwise trusts any inherited SIGNING_MODE). Unset Developer ID/API-key
and Apple-ID/notary variables, ANY_DEV_SIGNING_IDENTITY, ANY_ALLOW_UNNOTARIZED,
CI, GITHUB_ACTIONS, ANY_RELEASE_BUILD and SENTRY_AUTH_TOKEN without reading or
printing their values. Source evidence: signing/signing.sh resolve_signing_mode
and derive_apple_mode; vite.config.ts only uploads maps in pipeline/release
mode with its token. Avoid release.sh/setup_signing_keychain entirely. Ad-hoc
codesigning the task artifact uses no real OS credential store.

Keep this handoff narrow and development-only: use existing build/deploy
primitives and a small supervised launcher/setup helper only as needed. Target
this macOS arm64 host for the concrete artifact; do not invent untested Linux
or Windows packaging paths. Do not alter production startup, provider policy,
shared overlay defaults or release settings just to make the fixture run.
If a real product defect is found, report its scope before changing it so the
appropriate source checks and review coverage can be reopened explicitly.
Record any tracked-file drift from existing generation/build hooks rather than
silently treating generated source changes as part of the approved Step 8 tree.

## Active workspace and evidence

Product/helper/documentation work is confined to the paired worktrees under
/Users/roma/anytype/anybao/.worktrees/chatgpt-oauth-main: anybao and any-ui,
both feat/chatgpt-oauth-main. Read applicable AGENTS/playbooks and the Step 7/8
plans, current fresh-OAuth spec and docs/chatgpt-local-oauth.md. Original
checkouts and their concurrent Android work remain untouched. Root owns this
plan/progress ledger and adjudication; report any needed product scope change.

Read /private/tmp/anybao-oauth-main-evidence/step8-round2-adjudication.md,
step8-r1-repair-report.md, step8-build-environment.md, packaged-backend.md and
original-checkout-preservation.md. Step 8 frozen manifests and full patches are
step8-round2-{ui,bao}-freeze.json/patch. They are the approved product baseline.
No code changes after that gate should be hidden in packaging output.

Deliver actual FINAL with changed-path inventory, final artifact and SDK/source
identity, precise build/setup/verification commands and logs, safe launcher
regressions, deployed guest readback proof, and a concise user launch command.
Document exactly what was exercised without launching the GUI and what remains
manual. Root will freeze the complete paired delta and assign the three
independent lenses before telling the user the handoff is ready.

## Round 1 documentation repair — active 2026-11-03

All three independent Astra xhigh reviewers reached FINAL with zero C/H/M.
Root read all reports and re-froze the complete UI110/Bao35 deltas; both JSON
manifests stayed identical. Integration reported one Low navigation error.
Root confirmed SettingsModel.tsx renders “ChatGPT subscription” → “Set up
ChatGPT”, whereas the runbook incorrectly reused Welcome's “ChatGPT (fresh
sign-in)” label. Full adjudication: task evidence step9-round1-adjudication.md.

Resume the same Astra max implementer to correct only the runbook's manual
Settings path to Settings → Model → ChatGPT subscription → Set up ChatGPT →
Connect ChatGPT, retaining model selection and Apply ChatGPT model. Check
actual renderer strings and document formatting. Preserve every executable,
helper/program/source input, launch command and named-fixture state. This
document-only correction needs no app rebuild or repeated broad test suite.
Report exact changed path/diff and checks, then actual FINAL/freeze. Root will
verify the narrow delta and ask all three lenses for focused closure on the
documentation fix and retained source/artifact identities. User still owns
all GUI, live OAuth, CLI discovery and inference acceptance.
