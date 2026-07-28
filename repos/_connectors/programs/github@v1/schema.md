Field names follow the GitHub REST API (trimmed subsets — kept keys
are the API's own; `repo`, `is_pr`, `text`, `kind` are derived).

### whoami() [getter]
Verify the token; returns `{ok, login, id, name, html_url}` for the
authenticated user.

### list_my_issues(filter?, state?, since?, per_page?) [getter]
Issues + PRs involving the user, most recently updated first.
`filter`: "assigned" (default) | "created" | "mentioned" |
"subscribed" | "all". `state`: "open" (default) | "closed" | "all".
`since`: ISO-8601 lower bound on updated_at. `per_page` defaults 30,
capped 100. Returns `{ok, items}` — item shape: `{number, title,
state, html_url, repository_url, repo, user: {login}, labels, is_pr,
updated_at, body}` (body trimmed to 2000 chars).

### list_repo_issues(owner, repo, state?, since?, per_page?) [getter]
One repo's issues + PRs, same options and item shape.

### search_issues(q, per_page?) [getter]
Issue/PR search with GitHub search syntax (e.g. "repo:o/r is:pr
is:open review-requested:@me"). Budget 30 requests/min — use
sparingly. Returns `{ok, totalCount, incompleteResults, items}`.

### get_issue(owner, repo, number) [getter]
One issue/PR plus its comments: `{ok, issue, comments: [{user:
{login}, created_at, body}]}` (comment bodies trimmed to 1500 chars).

### list_pull_requests(owner, repo, state?, base?, per_page?) [getter]
One repo's PRs, most recently updated first. `state`: "open" (default)
| "closed" | "all"; `base` filters by target branch. Returns `{ok,
items}` — item shape: `{number, title, state, merged_at, draft,
html_url, repo, user: {login}, head: {ref}, base: {ref}, updated_at,
body}` (body trimmed to 2000 chars).

### get_pull_request(owner, repo, number) [getter]
One PR: `{ok, pr, files, reviews}`. `pr` = the list shape plus
`{additions, deletions, changed_files, commits, mergeable, merged_by:
{login}}`; `files`: up to 100 of `{filename, status, additions,
deletions}`; `reviews`: up to 50 of `{user: {login}, state,
submitted_at, body}`. Conversation comments: `get_issue` with the
same number.

### list_commits(owner, repo, sha?, path?, since?, per_page?) [getter]
Recent commits, newest first. `sha` = branch/tag/sha to walk from
(default branch if omitted); `path` filters to one file/dir; `since`
ISO-8601. Returns `{ok, items: [{sha, commit: {message, author:
{name, date}}, author: {login}, html_url}]}` (sha shortened to 12,
message first line).

### list_repos(affiliation?, sort?, per_page?) [getter]
Repos the user owns / collaborates on. `sort`: "pushed" (default) |
"created" | "updated" | "full_name"; `affiliation`: comma-set of
"owner" | "collaborator" | "organization_member" (default all three).
Returns `{ok, items: [{full_name, private, fork, archived,
description, default_branch, language, stargazers_count,
open_issues_count, created_at, pushed_at, html_url}]}`.

### get_repo(owner, repo) [getter]
One repo's metadata: the list_repos shape plus `{topics,
forks_count}`.

### request(method, path, params?, body?) [mutator]
Raw authorized GitHub API call — the escape hatch for anything the
methods above don't wrap, including writes (e.g. `request("post",
"/repos/o/r/issues/1/comments", body={"body": "..."})`). `method`:
get|post|put|patch|delete; `path` starts with "/" and is pinned to
api.github.com (the credential never goes elsewhere); `body` is sent
as JSON. Returns `{ok, status, data}` (parsed JSON; `text` instead
when non-JSON or truncated past 20000 chars).

### list_notifications(all?, since?, per_page?) [getter]
The user's notification inbox, unread by default (`all=true` includes
read). Returns `{ok, items: [{id, reason, subject: {title, type,
url}, repository: {full_name}, updated_at, unread}]}`. Needs the
PAT's Notifications permission.

### get_readme(owner, repo) [getter]
The repo README decoded to text: `{ok, path, text}` (trimmed to 8000
chars).

### get_contents(owner, repo, path) [getter]
A file or directory. File → `{ok, kind: "file", path, text}` (decoded,
trimmed to 8000); directory → `{ok, kind: "dir", entries: [{name,
path, type}]}`.
