# Step 8 Round 1 repair

Activated 2026-11-03 after all three independent reviewers reached FINAL and
root verified unchanged paired inventories. Continue with the same GPT-6 Astra
max implementer. No Step 9 work until a new three-lens Astra xhigh gate clears.
All scope, custody and execution boundaries in the Step 8 plan still apply.

## Confirmed H1: real space identifiers

`src-tauri/src/chatgpt/selection.rs` accepts only alphanumeric, hyphen and
underscore identifiers. Any's actual space contract is `<CID>.<base36 key>`.
Preserve the full identifier, including its replication suffix, and accept
real transport-safe IDs. Retain framing/path protections against separators,
percent ambiguity, whitespace/CRLF, traversal segments and oversized input.
Use the packaged SDK and committed wire fixtures as the contract; avoid an
unnecessary new identifier format or dependency.

Add regression coverage using realistic dotted IDs across native authorization
and the selection bridge, with invalid framing controls. Demonstrate the paths
used by OAuth and existing API/CLI selections; tests must not merely stub away
the native validator with synthetic `space` identifiers.

## Confirmed M1: queued acquisition across lifecycle boundaries

`model_selection_acquire` authorizes before `spawn_blocking`, but the worker
only registers/captures its lifecycle epoch once it starts. Capture/register
revocable window/session intent before that queue boundary. A worker must
validate the captured generation rather than adopting a newer one. Reset must
invalidate the first acquisition even when no earlier lease existed.

Add deterministic scheduling barriers for a queued first acquisition across
close, reload and Any-session reset. Preserve explicit priority, automatic and
repair exclusion, and native settlement of already active writes through
renderer disposal. Do not expire or release an active HTTP write to fix an
inactive orphan; durable ambiguity quarantine and read-only recovery remain.

## Evidence and gates

Read all three reports under `/private/tmp/anybao-step8-r1-{security,lifecycle,integration}`
and root's adjudication under
`/private/tmp/anybao-oauth-main-evidence/step8-round1-adjudication.md`.
The lifecycle exact-code Tokio probe reproduces close, reload and reset; root
ran it independently. The same report and integration report reproduce H1.

Run the new regressions and affected existing native/renderer suites. The UI
base advanced during repair to ac38edf7e57dd568c81b66e16436c982e9474dc2,
adding four upstream chat/editor fixes in 13 disjoint files. Root verified all
105 port paths and hashes survived the fast-forward unchanged. Run the full
UI suite again on this new combination; that run includes upstream regression
tests, so an additional duplicate run is unnecessary unless diagnosing a failure.
Complete applicable native safe-suite, Clippy, formatting,
generated IPC and required diff-aware lint gates. Never run real keychain or
ignored live tests. Keep precise commands, counts and limitations in scratch.

Reach actual FINAL with a complete changed-path inventory and concise delta
report. Root then freezes both repos and resumes all three independent review
lenses. Every new or unresolved Critical/High/Medium finding requires another
repair by the same implementer before proceeding.
