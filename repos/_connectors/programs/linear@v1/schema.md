### whoami() [getter]
Current Linear user `{ok, user: {id, name, email}}`; doubles as the
key validator. `{ok: false, error}` when not connected or rejected.

### my_issues(first?, after?) [getter]
Issues assigned to the current user, newest-first by updatedAt.
`first` defaults 25, capped 100. Returns `{ok, issues, hasNextPage,
endCursor}` — pass `after=endCursor` to page. Issue shape: `{id,
identifier, title, priority, url, createdAt, updatedAt, state: {name,
type}, assignee: {id, name}, team: {id, name, key}}`.

### list_issues(first?, after?, updated_after?) [getter]
Issues across the whole workspace, newest-first. `updated_after`
(ISO-8601) keeps only issues with updatedAt >= it — the incremental
form. Same return shape as my_issues.

### list_teams() [getter]
Teams `{ok, teams: [{id, name, key}], hasNextPage, endCursor}` — one
page of up to 100 (the structural map is small).

### get_issue(id) [getter]
One issue with full detail including `description` (Markdown). `id`
accepts the UUID or the human identifier ("ENG-123"). Returns `{ok,
issue}`.

### list_comments(issue_id, first?, after?) [getter]
Comments on an issue, oldest-first. Returns `{ok, issueId, identifier,
comments: [{id, body, createdAt, updatedAt, url, user: {id, name}}],
hasNextPage, endCursor}`.

### update_issue(id, title?, description?, state_id?, assignee_id?, priority?) [mutator]
Update fields on one issue. `id` MUST be the issue's UUID (from a
fetched issue's `id` field) — the mutation does not resolve human
identifiers. Only passed fields are touched; `priority` is Linear's
0-4 scale. Returns `{ok, issue}` (full detail, post-update).

### create_comment(issue_id, body) [mutator]
Post a Markdown comment on an issue. `issue_id` is the UUID. Returns
`{ok, comment: {id, body, createdAt, updatedAt, url, user}}`.
