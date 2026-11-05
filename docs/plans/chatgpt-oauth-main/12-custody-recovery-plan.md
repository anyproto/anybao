# Step 12 — explicit local ChatGPT custody recovery

## Report and evidence

The user sees “Your secure store is locked. Unlock it, then check the connection
again”, sees no Keychain dialog, deletes the relevant Keychain entries, restarts,
and reports the same error. This follows the compact Provider preview. There is
no new screenshot or external ticket for this report. We have not inspected the
user's profile, credentials, Keychain, app processes or logs; the particular OS
failure on their computer remains unconfirmed.

Source confirms two defects: the macOS helper suppresses all interaction, but
the UI supplies only passive retry; and missing wrapping keys become generic
invalid records while disconnect requires decrypting and rewriting that record.
Restart and fresh OAuth cannot repair either missing transition. A deleted
wrapping key cannot decrypt its ciphertext. Background custody must stay silent.

Before this step, complete UI120/Bao37 inventories exactly match Step 11 delivery
(69992a1637fb2da0d127b733c10ae52c861249a59270e1b227a468873beffad1;
5ab6bf4eaf5452457813896a63403ae6728c0c4fff0765decf7301e9da8d1977).

## Fault model

- Invariant: an explicit user action can authorize local custody, and a proven
  unrecoverable local ChatGPT record has a scoped, explicit recovery path.
- Class: missing state-machine transitions and lossy error classification.
- Owners: native custody/helper policy and core slot transactions; the UI only
  presents safe states and initiates the authorized action.
- Causal chain: interaction is denied in the helper and query; denied access is
  labeled locked; the UI never requests interactive access. A missing wrapping
  key cannot satisfy the read required by both login and disconnect.

## Implementation scope

1. Add deterministic failing repro tests before changes. Cover access requiring
   interaction and a persisted ciphertext whose fake wrapping key is removed.
   Use fake OS adapters/private scratch directories only, never real custody.
2. Distinguish denied/required access, missing key, corrupt record and unavailable
   service sufficiently for truthful recovery. Preserve safe closed enums;
   neither OS diagnostics nor any secret crosses IPC or logs.
3. Add explicit user-initiated access authorization. Background status, refresh,
   inference, startup and polling remain bounded/noninteractive. Use an isolated
   helper operation with appropriate bounded lifetime and cancellation; do not
   toggle UI-process global policy or route interactive waits through the normal
   three-second background wrapper. Preserve helper parent/code/pipe validation,
   native session/window/origin authorization and stale-intent fencing. Support
   first-key creation as well as access to an existing key where necessary.
4. Add explicit scoped recovery for an unreadable ChatGPT record. Explain that
   signing in again is required, obtain the user's confirmation in the product,
   and change only the current canonical local ChatGPT slot. Never delete other
   keys, reset the OS Keychain, silently replace corruption, or import CLI tokens.
   Fencing must cover pending OAuth/refresh, other windows/processes, disconnect,
   cancellation, logout and late completions. Ordinary valid grants, unknown
   outer formats and decodable unknown inner schemas must not be discarded.
   With a missing key the inner schema is unknowable; explicit confirmed reset
   of the exact recognized outer record is permitted and requires fresh login.
5. Keep Provider compact: one relevant recovery action and concise truthful copy;
   cancellation/denial stays recoverable. Share controller behavior with welcome
   and Credentials. Browser/non-macOS behavior must be explicit and safe. Update
   owning specs, custody contract, compatibility table and generated IPC as needed.
6. Run focused failing-then-passing regressions, affected UI/native/core checks,
   typecheck, generated IPC parity, formatting, Clippy and required lint gates.
   No real-Keychain/ignored live tests, GUI, OAuth, inference or CLI discovery.

## Execution and review

One GPT-6 Astra max implementer owns product/test/spec edits. Root owns this
plan, progress and evidence. Use the existing paired isolated worktrees; do not
change originals, commits or branches. User-owned testing and no-live-access
boundary override local playbook GUI/commit suggestions. No unrelated changes.

After actual implementation and runnable-handoff FINAL, freeze source/artifacts
and run three independent
GPT-6 Astra xhigh reviews: custody/security, state/lifecycle, and integration/UX.
Root confirms or rejects every finding with evidence, resumes the same
implementer for confirmed findings and repeats all three until zero C/H/M.

## Runnable handoff included in this step (updated after testing request)

The user asks for a dev-start command and to test manually. Browser `pnpm go`
does not exercise native OAuth. Include the formerly separate Step 13 handoff
in this step, under the same Astra max implementer and the same final three-lens
gate. This changes sequencing, not the review requirement. Complete source
checks first, then prepare the concrete runnable artifact before FINAL.

1. Preserve both existing bundles byte-for-byte: `Any.app` and
   `Any Provider Preview.app` in the task target directory. Build a distinct
   `Any Recovery Preview.app` with only a scratch productName overlay, retaining
   the same identifier, executable, profile derivation and custody namespace.
   Never execute an app, inspect a live process/profile or modify installed apps.
2. The Step 11 metadata recipe deliberately forbids native/Bao changes. Keep its
   v1 validation strict. Add an explicit, reviewed native-update recipe contract
   rather than bypassing that guard. It may accept the two exact known prior
   source/bundle endpoints from public Step 9/11 evidence. Unknown states fail.
   Preserve the required recipe digest, complete source/artifact identities,
   fixed account/device/profile/deployment metadata and the atomic byte-preserving
   transaction. A validated no-op must still perform all checks.
3. Permit only native-host Bao source changes for this compatible update; guest
   program sources, kernel, SDK and all deployment/namespace inputs remain fixed.
   The old standalone anyrt executable is the initial deploy CLI, unused on
   resume; preserve it and its hash. New core code is compiled into the desktop.
   Any guest/kernel/SDK or profile-identity change requires redesign, not silent
   deployment or repair. All endpoint generation comes from public evidence and
   source/artifact reads, never the user's manifest or credentials.
4. Update remains offline and metadata-only: no app/backend startup, auth,
   network request, deployment, credential reset, identity/vault/config/log or
   launch-state access. Preserve private/canonical path checks, no links,
   exclusive lock, occupied-port refusal, interruption checks, atomic rename and
   directory sync. Only reviewed build fields change; all other bytes survive.
5. Exercise update/no-op, wrong endpoint/hash/source/artifact/namespace, lock/port
   refusal, interruptions and preservation on fresh synthetic fixtures only.
   Verify the actual CLI on a fake root. Do not prepare another real account.
6. Build with the pinned toolchain, explicit paired ANYBAO_DIR, bounded environment
   and ad-hoc signing; no release/upload/notary/signing-certificate operations.
   Preserve both old bundle inventories and pinned SDK/kernel/CLI artifacts.
   Record new complete bundle/source hashes, signature, architecture/macOS target,
   embedded kernel, generated IPC parity and exact recipe digest. Use distinct
   candidates for any later repair, preserving reviewed round identities.
7. Update the user runbook and provide one explicit quit/wait, metadata-update,
   then separate resume command. Manual acceptance is Provider access/reset,
   fresh sign-in if needed, inference, model choice and restart. Code/tests and
   signatures do not claim actual Keychain/dialog or visual acceptance.

The integration review includes packaging/provenance and exact commands; the
security and lifecycle reviews also cover the metadata transaction. No agent may
inspect or operate `/private/tmp/anybao-oauth-main-handoff` or anything beneath
it. The user alone performs its update, launch and confirmed connection reset.
