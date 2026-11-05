# Closeout and deferred provider navigation

2026-11-05. Documentation, branch publication and review coordination.

1. Record the deferred navigation problem in the owning UI spec: local CLI
   choices appear nested under provider choices, and the ChatGPT subscription
   controls make the hierarchy harder to understand. Keep both alternatives
   open: put **On this computer** alongside the provider selector, or group
   Codex under OpenAI and Claude Code under Claude. Decide the ChatGPT sign-in
   placement as part of that future design. No current UI change.
2. Record manual acceptance of the latest macOS preview without expanding it
   into unperformed platform, recovery, or release acceptance.
3. Use the Astra max implementer and three Astra xhigh documentation reviews
   (product intent, validation/lifecycle, repository/handoff). Resolve any
   Critical, High or Medium finding before creating delivery commits.
4. Verify every product source byte against the Step 13 delivery inventory.
   Preserve the preview worktrees' HEAD, branch and index: the local launcher
   checks their exact original identities. Create local `feat/chatgpt-oauth`
   delivery branches in both repositories using separate temporary indexes and
   verified trees. Push both branches, open two linked PRs, and coordinate
   review with Tolya through any-agents. No merge or release is included.
5. Describe new ChatGPT sign-in support: users can connect without installing
   Codex or Claude Code, and device-code authorization supports setup when a
   browser is on another device. Explain the paired runtime/UI dependency and
   the headless integration boundary. Do not frame the feature as a refactor
   of previously shared OAuth support.

The existing automated gates and reviews remain evidence for unchanged code.
Run documentation and whitespace checks for this closeout. Do not read or
operate the live preview root, credentials, apps or running processes. No new
build or live acceptance run is needed for documentation and Git snapshots.
