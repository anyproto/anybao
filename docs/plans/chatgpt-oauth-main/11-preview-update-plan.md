# Step 11 — Separate preview build and profile-preserving handoff

Prepare a runnable preview containing the reviewed Provider simplification,
without touching the user's currently connected profile or overwriting the
previous Any.app. Follow the same Astra max implementation / three independent
Astra xhigh review / root adjudication loop. Root activated this plan on 2026-11-04 after
all three Step10 lenses reached actual FINAL with no C/H/M or Low findings. No commit, push, release or installed app change.

## Artifact

- Reuse the paired worktrees and pinned toolchain/cache. Read applicable AGENTS,
  desktop-shell playbook, current handoff runbook and Step9 build evidence.
- Build a separate task-local app bundle, using an untracked scratch Tauri
  productName overlay (candidate `Any Provider Preview`). Keep the base bundle
  identifier `io.anyproto.any`, main binary and exact ANY_DATA_DIR behavior.
  Do not change tracked release/packaging configuration. Confirm productName
  cannot change custody, webview or single-instance namespace.
- Use canonical `pnpm tauri:debug --bundles app --config <scratch overlay>` with
  paired ANYBAO_DIR and bounded build environment, SIGNING_MODE=adhoc and no
  release/upload/notarization/Apple/dev-signing variables. Preserve app runtime
  PATH/HOME/CODEX_HOME; never use the build environment to launch the app.
- Do not overwrite `/private/tmp/anybao-oauth-main-tools/cargo-target/debug/bundle/macos/Any.app`.
  Hash its complete bundle before and after; compare with Step9 public evidence.
  Never execute either app. Never stop the user's processes.
- Keep anyrt, staged SDK, Bao source, guest kernel and local program inventories
  unchanged. The canonical kernel prebuild may check freshness; do not force a
  kernel/anyrt rebuild or guest deployment for a renderer-only change. If any
  pinned guest/native artifact differs unexpectedly, report before proceeding.
- Verify new full bundle, architecture/minimum macOS, ad-hoc signature, packaged
  SDK normalized content and embedded unchanged kernel. Record source provenance
  and final artifact hashes. Existing task-only SwiftPM output-directory shim
  may be reused if needed; no dependency/build-policy patch to work around it.

## User-invoked metadata update

The prior manifest correctly rejects changed sources on restart. Add a narrow
explicit update operation to the existing local handoff helper (or a small
module it calls), tested on synthetic fixtures. This is development tooling,
not a general migration, source bypass or product/auth change.

1. A public-safe recipe binds the exact reviewed old and new complete source /
   bundle metadata. Build it from `step9-handoff-manifest-public.json` plus the
   new artifact and current reviewed source inventory, never from the live root.
   Put generated recipe/evidence in task scratch, not compiled source. Keep
   source/artifact checks fail-closed; do not accept arbitrary current files as
   the old baseline or silently rebaseline a stale build.
2. The user explicitly invokes update on an idle root. Verify private regular
   root/manifest, acquire existing exclusive run.lock before the transaction,
   refuse an occupied loopback port, active/stale lock, malformed recipe,
   symlinks, wrong paths/bases or mismatched old/new source/artifact records.
   No forced unlock, process kill, port reassignment or profile fallback.
3. Atomically replace only reviewed build metadata (`app`, `executable`, `sdk`,
   `bundle`, `sources` and any narrowly necessary existing build field). Preserve
   all remaining manifest fields. Keep launch-state, identity, profile, config,
   overlays, account/device, readback, kernel and anyrt byte-identical. Never read
   identity.json, vault, keychain, CLI credentials, recovery material or app logs.
   No backend boot, auth, network request, guest deployment or GUI operation.
4. An interrupted/failed update leaves the old complete record or the new
   complete record. No partially rewritten manifest or first-launch rearming.
   Explicit `--resume-fixture` remains required for the separate subsequent
   launch. An already-updated recipe may report a validated no-op or clearly
   refuse without changes. Keep output/errors bounded and secret-free.
5. The exact profile/root and native namespace stay fixed. Do not copy, import,
   export or reset any OAuth token. A rebuild may prompt for macOS Keychain
   access; real custody acceptance is manual, not permission to alter policy.

Agents MUST NOT invoke update/verify/launch or any other operation against
`/private/tmp/anybao-oauth-main-handoff`. It now contains the user's real
connection. Do not read that root, its children or their logs, even to inspect
lock/state. All update tests use fake manifests/profiles/bundles under a fresh
task scratch root. The final user command, after they quit the old app/launcher,
performs the actual update and explicit resume.

## Tests, docs and handoff

- Meaningful fake-fixture tests: valid update changes only allowed metadata;
  unknown fields and launch-state survive; identity/vault reads are forbidden;
  wrong old/new inventories, unchanged-guest mismatches, unsafe paths/permissions,
  active lock/port and atomic failure refuse without partial mutation. Inspect
  existing helper tests and reuse seams; no real app/backend/profile required.
- Retain prepare/verify/launch semantics, owned-child cleanup, runtime env and
  explicit restart safeguards. Run helper tests, pertinent formatting/lint and
  required lint:push after changes; no whole UI/native/Python rerun for tooling.
- Update the handoff runbook with exact Provider navigation and a short explicit
  user update then resume sequence. Say to quit the old app/launcher first; never
  tell users to remove stale locks blindly, reset a profile or paste auth tokens.
- Implementation actual FINAL with full paired changed/untracked inventories,
  Step11 delta, source freeze, logs, old/new artifact provenance, recipe and its
  hash, precise runnable commands and manual limitations. Only then review.
- Three lenses: update safety/credential isolation, transaction/lifecycle,
  build provenance/user handoff. Root independently confirms/rejects every
  finding; same implementer repairs; repeat all three until zero C/H/M. Source
  changes after build require honest rebuild/rebinding of recipe as applicable.

No live UI, OAuth, OS custody, CLI discovery or inference acceptance claim.
Final delivery must be runnable by the user and preserve their existing state.
