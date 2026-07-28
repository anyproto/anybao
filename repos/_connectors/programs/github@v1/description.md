GitHub connector: your assigned issues and PRs, repo issues, targeted
issue/PR search, one issue with comments, pull requests proper (branch
refs, draft/merged; detail adds changed files + review verdicts),
recent commits, your repos + repo metadata, the notification inbox,
and repo README / file contents (decoded, trimmed to keep context
small). For anything else on api.github.com there is `request()` — a
raw authorized call (the one write path; the credential is pinned to
api.github.com). Result fields keep the GitHub API's own names
(html_url, updated_at, user.login, …) — trimmed subsets, not renames. All methods return `{ok, ...}` or `{ok: false,
error}` with actionable messages — a missing/expired token comes back
as an error explaining how to connect, never a traceback. Auth is a
host-injected fine-grained PAT (`connector.key.github`); the token
never enters guest code or the trace. Rate limits: search 30/min (use
search_issues sparingly), other reads 5000/hr — brief 429 backoff is
built in.
