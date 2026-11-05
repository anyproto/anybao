# Bring the published feature up to current main and repair CI

2026-11-05. The paired feature branches and PRs are published. Updating the
existing paired worktrees is authorized. Both now check out `feat/chatgpt-oauth`.

- Bao published head: `efda9465d62ee74f84d27d633c0f89450d17b4c7`.
  Current main remains `fb1de0f0732e296693116b1b0e80f64d7485ad1d`.
- UI published head: `726c3e3f8ebecc2d905166c8935f1aaa0c808685`.
  Current main is `3310eec46` (resolve the full fetched ref before merging).
- Bao PR 142 runtime CI passed Clippy and formatting, then failed the license
  input/notice manifest check. UI PR 3002 conflicts with current main.

## Implementation

1. Inspect the added/changed Rust dependencies and the repository's license
   generation instructions. Update the reviewed notices and manifest as needed;
   pass `make licenses-check` without disabling or weakening the gate.
2. Merge the fetched UI main into the published feature branch without rewriting
   history. Resolve overlaps preserving both upstream behavior and the new
   independent ChatGPT provider, credential boundaries, model choices and local
   CLI support. Do not implement the deferred Provider navigation redesign.
3. Run relevant merged-surface tests, typecheck, native compile/checks where
   affected, formatting and the normal pre-push checks. Use pinned toolchains.
   Investigate failures rather than claiming previous results cover merged code.
4. Read the impact on the old pinned local-preview handoff. Keep its identity
   guards intact and explain any obsolete launch prerequisites in its runbook.
   A release or replacement of the live preview is not part of this CI repair.
5. Freeze the final changes for three independent Astra xhigh lenses after
   Astra max implementation. Root confirms/rejects findings; the same implementer
   repairs confirmed findings until no Critical/High/Medium issue remains.
6. Root commits and pushes the reviewed fixes/merge, checks CI, updates both PRs
   and continues the existing Tolya collaboration with the new exact heads.

Keep the shared original checkouts, local OAuth data, live preview root,
credentials and running apps outside automated access. No merge of either PR
or release is authorized. A standalone headless setup command and navigation
redesign remain separate work.

## External draft-review follow-up

The paired PR review adds these confirmed repairs to this step before its
three-lens gate:

- Production refresh must preserve a grant after a failure proven to precede
  sending the request. The pinned transport's DNS/connect classification must
  be checked against its implementation. Later I/O, response-read ambiguity,
  and HTTP 429/5xx do not prove that rotation did not occur and remain closed
  to retry with the old token.
- Ignore bounded, unknown nonterminal Responses events while preserving all
  validation of consumed items, tool arguments, terminal completion and EOF.
- Retry transient device-code poll transport/5xx failures with bounded backoff,
  cancellation and the original deadlines. Do not retry the authorization-code
  exchange on ambiguity. Treat an explicit slow-down response conservatively
  without claiming that the private endpoint is known to emit it.

- For an unrecognized non-streaming HTTP 401, use the existing one-recovery
  budget after known quota/policy/residency exclusions. A second such rejection
  requires reconnect for the still-current pair; this means the freshly
  authorized credentials remain unusable for inference, not that server-side
  refresh revocation is proven. Preserve streaming/error sanitization and
  replacement-grant fences, and document that availability policy explicitly.
- Reject stray mismatched browser callback requests and keep listening within
  the original bounds. Matching-state denial, cancellation and expiry still end
  login. Restore reasoning extraction for truncated replies from all providers
  while retaining the prohibition on turning truncated text into tool calls.

The remaining Low findings are explicit follow-ups/limitations: stale foreign
pending login expires at its existing TTL; unused cooldown adds no permission
to replay a refresh; readiness optimization must preserve freshness and avoid
plaintext-key caching; ambiguous preset/local writes need fresh receipts and
full verification wiring, not a relaxed `$unset` check; failed disconnect is
process-local until durable removal succeeds and must be retried.

App-wide raw minidump removal is a real diagnostics tradeoff;
the explicit policy question is pending, and current documentation needs to
match whichever policy is chosen. A coordinated Bao/UI merge window is also
required because the added runtime dependencies change the UI lockfile; this
agent is not authorized to merge PRs or schedule releases.
