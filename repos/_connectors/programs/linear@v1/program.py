"""Linear issue-tracker connector (GraphQL) — read issues, two writes.

Your assigned issues, workspace issues (optionally incremental by
updatedAt), teams, one issue with full description, issue comments;
writes: update_issue and create_comment. Methods return {ok, ...} or
{ok: False, error} with actionable messages — a missing/rejected API
key explains how to connect, never a traceback. Linear mutations need
the issue UUID, not "ENG-123" — fetch the issue first."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The personal API key never enters the guest: every request names
# `credential: {ref: "connector.key.linear"}` and the host injects
# the Authorization header after recording (anybao ADR-008 §1).
# Linear personal keys go RAW in Authorization (NO "Bearer " prefix —
# that's OAuth-only; a Bearer prefix on a personal key yields a 401
# that looks like a bad key).

import json

_ENDPOINT = "https://api.linear.app/graphql"
_KEY_URL = "https://linear.app/settings/api"
_CRED = {"ref": "connector.key.linear", "header": "Authorization"}
_DEFAULT_FIRST = 25
_MAX_FIRST = 100
_TIMEOUT_S = 60

_NOT_CONNECTED = (
    "Linear not connected — create a personal API key at " + _KEY_URL
    + ", then set LINEAR_API_KEY in the runtime environment once (serve "
    + "seeds the device-local secret store on start) or write a localValue "
    + "for connector.key.linear on the config object."
)


def _clamp_first(n):
    # Linear's complexity rate limit multiplies a connection's cost by
    # `first`, so a careless huge page can trip the per-query ceiling.
    if not isinstance(n, (int, float)) or n <= 0:
        return _DEFAULT_FIRST
    return min(int(n), _MAX_FIRST)


def _gql(query, variables):
    """One POST to the GraphQL endpoint → {ok, data} | {ok: False, error,
    status?}. Inspects BOTH the HTTP status AND body.errors — Linear
    (like most GraphQL servers) returns HTTP 200 with an `errors[]`
    array for many failures."""
    try:
        resp = http.post(_ENDPOINT, json={"query": query, "variables": variables or {}},  # noqa: F821
                         timeout=_TIMEOUT_S, credential=_CRED)
    except EffectError as e:  # noqa: F821 - guest global
        if "no secret for credential ref" in str(e):
            return {"ok": False, "error": _NOT_CONNECTED}
        return {"ok": False, "error": f"request failed: {e}"}
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status in (400, 401):
        return {"ok": False, "status": resp.status,
                "error": f"Linear rejected the API key (HTTP {resp.status}). "
                         f"The key is sent RAW, no Bearer prefix. Create a new key at "
                         f"{_KEY_URL} and re-seed connector.key.linear if needed."}
    if resp.status >= 300:
        msg = json.dumps(body["errors"]) if body and body.get("errors") else f"HTTP {resp.status}"
        return {"ok": False, "status": resp.status, "error": msg}
    if body and body.get("errors"):
        return {"ok": False, "status": resp.status,
                "error": "Linear: " + json.dumps(body["errors"])}
    if not body or not body.get("data"):
        return {"ok": False, "status": resp.status, "error": "empty GraphQL response"}
    return {"ok": True, "data": body["data"]}


def _conn(connection):
    """Normalize a Relay connection into (nodes, hasNextPage, endCursor)."""
    connection = connection or {}
    pi = connection.get("pageInfo") or {}
    return (connection.get("nodes") or [], bool(pi.get("hasNextPage")),
            pi.get("endCursor"))


_ISSUE_FIELDS = ("id identifier title priority url createdAt updatedAt "
                 "state { name type } assignee { id name } "
                 "team { id name key }")
_ISSUE_FIELDS_FULL = _ISSUE_FIELDS + " description"


@span("linear.whoami", kind="getter")  # noqa: F821 - guest global
def whoami():
    """Current Linear user; doubles as the key validator."""
    r = _gql("query Whoami { viewer { id name email } }", {})
    if not r["ok"]:
        return r
    v = r["data"].get("viewer")
    if not v:
        return {"ok": False, "error": "no viewer in response (key may be invalid)"}
    return {"ok": True, "user": {"id": v["id"], "name": v.get("name"), "email": v.get("email")}}


@span("linear.my_issues", kind="getter")  # noqa: F821 - guest global
def my_issues(first=None, after=None):
    """Issues assigned to the current user, newest-first.

    Returns `{ok, issues, hasNextPage, endCursor}` — pass
    `after=endCursor` to page. Issue shape: `{id, identifier, title,
    priority, url, createdAt, updatedAt, state: {name, type}, assignee:
    {id, name}, team: {id, name, key}}`.
    """
    q = ("query MyIssues($first: Int!, $after: String) {"
         " viewer { id assignedIssues(first: $first, after: $after, orderBy: updatedAt) {"
         " nodes { " + _ISSUE_FIELDS + " } pageInfo { hasNextPage endCursor } } } }")
    r = _gql(q, {"first": _clamp_first(first), "after": after})
    if not r["ok"]:
        return r
    nodes, more, cursor = _conn((r["data"].get("viewer") or {}).get("assignedIssues"))
    return {"ok": True, "issues": nodes, "hasNextPage": more, "endCursor": cursor}


@span("linear.list_issues", kind="getter")  # noqa: F821 - guest global
def list_issues(first=None, after=None, updated_after=None):
    """Issues across the workspace, newest-first. `updated_after`
    (ISO-8601) makes it incremental: only updatedAt >= it.

    .
    """
    variables = {"first": _clamp_first(first), "after": after}
    if updated_after:
        variables["since"] = updated_after
        q = ("query ListIssues($first: Int!, $after: String, $since: DateTimeOrDuration) {"
             " issues(first: $first, after: $after, orderBy: updatedAt,"
             " filter: { updatedAt: { gte: $since } }) {"
             " nodes { " + _ISSUE_FIELDS + " } pageInfo { hasNextPage endCursor } } }")
    else:
        q = ("query ListIssues($first: Int!, $after: String) {"
             " issues(first: $first, after: $after, orderBy: updatedAt) {"
             " nodes { " + _ISSUE_FIELDS + " } pageInfo { hasNextPage endCursor } } }")
    r = _gql(q, variables)
    if not r["ok"]:
        return r
    nodes, more, cursor = _conn(r["data"].get("issues"))
    return {"ok": True, "issues": nodes, "hasNextPage": more, "endCursor": cursor}


@span("linear.list_teams", kind="getter")  # noqa: F821 - guest global
def list_teams():
    """Teams {id, name, key}. One page (the structural map is small)."""
    q = ("query ListTeams($first: Int!) { teams(first: $first) {"
         " nodes { id name key } pageInfo { hasNextPage endCursor } } }")
    r = _gql(q, {"first": _MAX_FIRST})
    if not r["ok"]:
        return r
    nodes, more, cursor = _conn(r["data"].get("teams"))
    return {"ok": True, "teams": nodes, "hasNextPage": more, "endCursor": cursor}


@span("linear.get_issue", kind="getter")  # noqa: F821 - guest global
def get_issue(id):
    """One issue with full detail (includes description Markdown). `id`
    accepts the issue UUID or its identifier (e.g. "ENG-123").

    Returns `{ok, issue}`.
    """
    if not id or not isinstance(id, str):
        return {"ok": False, "error": "id is required"}
    q = "query GetIssue($id: String!) { issue(id: $id) { " + _ISSUE_FIELDS_FULL + " } }"
    r = _gql(q, {"id": id})
    if not r["ok"]:
        return r
    issue = r["data"].get("issue")
    if not issue:
        return {"ok": False, "error": f"issue not found: {id}"}
    return {"ok": True, "issue": issue}


@span("linear.list_comments", kind="getter")  # noqa: F821 - guest global
def list_comments(issue_id, first=None, after=None):
    """Comments on an issue, oldest-first (UUID or identifier).

    Returns `{ok, issueId, identifier, comments: [{id, body, createdAt,
    updatedAt, url, user: {id, name}}], hasNextPage, endCursor}`.
    """
    if not issue_id:
        return {"ok": False, "error": "issue_id is required"}
    q = ("query ListComments($id: String!, $first: Int!, $after: String) {"
         " issue(id: $id) { id identifier comments(first: $first, after: $after) {"
         " nodes { id body createdAt updatedAt url user { id name } }"
         " pageInfo { hasNextPage endCursor } } } }")
    r = _gql(q, {"id": issue_id, "first": _clamp_first(first or 50), "after": after})
    if not r["ok"]:
        return r
    issue = r["data"].get("issue")
    if not issue:
        return {"ok": False, "error": f"issue not found: {issue_id}"}
    nodes, more, cursor = _conn(issue.get("comments"))
    return {"ok": True, "issueId": issue["id"], "identifier": issue.get("identifier"),
            "comments": nodes, "hasNextPage": more, "endCursor": cursor}


@span("linear.update_issue", kind="mutator")  # noqa: F821 - guest global
def update_issue(id, title=None, description=None, state_id=None,
                 assignee_id=None, priority=None):
    """Update fields on one issue (Linear `issueUpdate`); id = UUID.

    `id` is the issue's UUID (the `id` field on a fetched issue), NOT
    the human identifier — the mutation does not resolve "ENG-123"; grab the UUID
    from get_issue / my_issues / list_issues first. Only the fields you
    pass are touched; `priority` is Linear's 0-4 scale.

    Returns `{ok, issue}` (full detail, post-update).
    """
    if not id or not isinstance(id, str):
        return {"ok": False, "error": "id is required (the issue's "
                "UUID, e.g. from get_issue(...).issue.id)"}
    input = {}
    if isinstance(title, str):
        input["title"] = title
    if isinstance(description, str):
        input["description"] = description
    if isinstance(state_id, str):
        input["stateId"] = state_id
    if isinstance(assignee_id, str):
        input["assigneeId"] = assignee_id
    if isinstance(priority, (int, float)):
        input["priority"] = int(priority)
    if not input:
        return {"ok": False, "error": "nothing to update — pass at least one of: "
                                      "title, description, state_id, assignee_id, priority"}
    q = ("mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {"
         " issueUpdate(id: $id, input: $input) {"
         " success issue { " + _ISSUE_FIELDS_FULL + " } } }")
    r = _gql(q, {"id": id, "input": input})
    if not r["ok"]:
        return r
    res = r["data"].get("issueUpdate")
    if not res or not res.get("success"):
        return {"ok": False, "error": "Linear rejected the update"}
    return {"ok": True, "issue": res.get("issue")}


@span("linear.create_comment", kind="mutator")  # noqa: F821 - guest global
def create_comment(issue_id, body):
    """Post a comment on an issue (Linear `commentCreate`). `issue_id`
    is the issue's UUID; `body` is Markdown.

    Returns `{ok, comment: {id, body, createdAt, updatedAt, url,
    user}}`.
    """
    if not issue_id or not isinstance(issue_id, str):
        return {"ok": False, "error": "issue_id is required (the issue's UUID)"}
    if not body or not isinstance(body, str):
        return {"ok": False, "error": "body is required"}
    q = ("mutation CommentCreate($input: CommentCreateInput!) {"
         " commentCreate(input: $input) {"
         " success comment { id body createdAt updatedAt url user { id name } } } }")
    r = _gql(q, {"input": {"issueId": issue_id, "body": body}})
    if not r["ok"]:
        return r
    res = r["data"].get("commentCreate")
    if not res or not res.get("success"):
        return {"ok": False, "error": "Linear rejected the comment"}
    return {"ok": True, "comment": res.get("comment")}


def main(args):
    return whoami()
