"""programs/linear@v1 through the REAL guest kernel (connectorenv
wires anybao's tests/kernelenv.py at this repo's programs/). Fixtures
are inline GraphQL response dicts modeled on live api.linear.app
replies (anyorg workspace, recorded 2026-07-28). Pinned here: the
envelope shapes ({ok, issues|states|users, hasNextPage, endCursor} —
node fields pass through with Linear's native camelCase names, C1),
credential plumbing (RAW key, no Bearer prefix), the GraphQL error
contract (HTTP 200 + body errors[] → {ok: False}), mutation input
mapping (snake_case args → Linear's camelCase input keys), and the
gql() escape-hatch passthrough."""

import json

from connectorenv import connector_kernel


def gql_ok(data):
    return {"status": 200, "headers": {},
            "body": json.dumps({"data": data}), "url": ""}


def gql_errors(errors, status=200):
    return {"status": status, "headers": {},
            "body": json.dumps({"errors": errors}), "url": ""}


ISSUE = {"id": "193c44aa-50d1-4a1b-bca3-5be9955ca868", "identifier": "SYN-92",
         "title": "guest invite: split the key", "priority": 3,
         "url": "https://linear.app/anyorg/issue/SYN-92/guest-invite",
         "createdAt": "2026-07-21T18:27:29.848Z",
         "updatedAt": "2026-07-22T13:34:15.821Z",
         "state": {"name": "Triage", "type": "triage"},
         "assignee": {"id": "ce86e9f9", "name": "Sergey"},
         "team": {"id": "0aaf6d21", "name": "Sync", "key": "SYN"}}
PAGE = {"hasNextPage": False, "endCursor": None}


class FakeLinear:
    """Routes on an operation-name substring of the posted GraphQL
    query — first match wins. An Exception value is raised host-side
    → guest EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []

    def __call__(self, name, payload):
        assert name == "http.post", f"unexpected effect {name}"
        self.requests.append(payload)
        query = (payload.get("json") or {}).get("query", "")
        for pattern, resp in self.routes:
            if pattern in query:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for query: {query[:80]}")


def load(routes):
    fake = FakeLinear(routes)
    ln = connector_kernel(effect=fake).use("linear@v1")
    return ln, fake


# ---- auth + transport contract ---------------------------------------------

def test_whoami_and_raw_key_plumbing():
    ln, fake = load([("Whoami", gql_ok(
        {"viewer": {"id": "u1", "name": "Anatolii", "email": "a@b.c"}}))])
    out = ln.whoami()
    assert out == {"ok": True, "user": {"id": "u1", "name": "Anatolii",
                                       "email": "a@b.c"}}
    req = fake.requests[0]
    assert req["url"] == "https://api.linear.app/graphql"
    # personal keys go RAW in Authorization — ref only, NO Bearer prefix
    assert req["credential"] == {"ref": "connector.key.linear",
                                 "header": "Authorization"}


def test_missing_secret_maps_to_connect_help():
    ln, _ = load([("Whoami", RuntimeError(
        'no secret for credential ref "connector.key.linear"'))])
    out = ln.whoami()
    assert out["ok"] is False
    assert "linear.app/settings/api" in out["error"]
    assert "LINEAR_API_KEY" in out["error"]


def test_graphql_errors_array_maps_to_error():
    ln, _ = load([("Whoami", gql_errors([{"message": "rate limited"}]))])
    out = ln.whoami()
    assert out["ok"] is False
    assert "rate limited" in out["error"]


def test_graphql_error_prefers_user_presentable_message():
    ln, _ = load([("GetIssue", gql_errors([{
        "message": "Entity not found: Issue", "path": ["issue"],
        "extensions": {"type": "invalid input", "code": "INPUT_ERROR",
                       "userError": True,
                       "userPresentableMessage":
                           "Could not find referenced Issue."}}]))])
    out = ln.get_issue("NOPE-999")
    assert out == {"ok": False, "status": 200,
                   "error": "Linear: Could not find referenced Issue."}


# ---- new getters (L2/L3/L4) -------------------------------------------------

def test_search_issues_shape():
    ln, fake = load([("SearchIssues", gql_ok(
        {"searchIssues": {"nodes": [ISSUE], "pageInfo": PAGE}}))])
    out = ln.search_issues("invite")
    assert out == {"ok": True, "issues": [ISSUE], "hasNextPage": False,
                   "endCursor": None}
    variables = fake.requests[0]["json"]["variables"]
    assert variables["term"] == "invite"


def test_search_issues_requires_term():
    ln, fake = load([])
    assert ln.search_issues("")["ok"] is False
    assert fake.requests == []


STATE = {"id": "dee31245", "name": "Done", "type": "completed",
         "position": 3, "team": {"id": "89186d03", "name": "iOS",
                                 "key": "IOS"}}


def test_list_states_all_and_team_filter():
    ln, fake = load([("ListStates", gql_ok(
        {"workflowStates": {"nodes": [STATE], "pageInfo": PAGE}}))])
    out = ln.list_states()
    assert out["ok"] is True and out["states"] == [STATE]
    assert "filter" not in fake.requests[0]["json"]["query"]

    out = ln.list_states(team_id="89186d03")
    assert out["states"] == [STATE]
    req = fake.requests[1]["json"]
    assert req["variables"] == {"teamId": "89186d03"}
    assert "filter: { team: { id: { eq: $teamId } } }" in req["query"]


def test_list_users_shape():
    user = {"id": "16d90191", "name": "kaye@anytype.io", "displayName": "kaye",
            "email": "kaye@anytype.io", "active": True}
    ln, _ = load([("ListUsers", gql_ok(
        {"users": {"nodes": [user], "pageInfo": PAGE}}))])
    out = ln.list_users()
    assert out == {"ok": True, "users": [user], "hasNextPage": False,
                   "endCursor": None}


# ---- create_issue (L1) ------------------------------------------------------

def test_create_issue_input_mapping():
    ln, fake = load([("IssueCreate", gql_ok(
        {"issueCreate": {"success": True, "issue": ISSUE}}))])
    out = ln.create_issue("0aaf6d21", "new issue", description="body md",
                          state_id="dee31245", assignee_id="ce86e9f9",
                          priority=4)
    assert out == {"ok": True, "issue": ISSUE}
    # snake_case args map onto Linear's camelCase input keys
    assert fake.requests[0]["json"]["variables"]["input"] == {
        "teamId": "0aaf6d21", "title": "new issue", "description": "body md",
        "stateId": "dee31245", "assigneeId": "ce86e9f9", "priority": 4}


def test_create_issue_requires_team_and_title():
    ln, fake = load([])
    out = ln.create_issue("", "x")
    assert out["ok"] is False and "team_id" in out["error"]
    out = ln.create_issue("t1", "")
    assert out["ok"] is False and "title" in out["error"]
    assert fake.requests == []


def test_create_issue_rejection():
    ln, _ = load([("IssueCreate", gql_ok(
        {"issueCreate": {"success": False, "issue": None}}))])
    out = ln.create_issue("t1", "x")
    assert out == {"ok": False, "error": "Linear rejected the create"}


# ---- gql escape hatch (L5) --------------------------------------------------

def test_gql_passthrough():
    ln, fake = load([("organization", gql_ok(
        {"organization": {"name": "anyorg", "urlKey": "anyorg"}}))])
    out = ln.gql("query { organization { name urlKey } }")
    assert out == {"ok": True, "data": {"organization": {
        "name": "anyorg", "urlKey": "anyorg"}}}
    assert fake.requests[0]["json"]["variables"] == {}


def test_gql_requires_query():
    ln, fake = load([])
    assert ln.gql("")["ok"] is False
    assert fake.requests == []
