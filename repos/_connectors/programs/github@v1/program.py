"""github@v1 — the user's GitHub work context (issues, pull requests
with files + reviews, commits, repos, notifications, README/contents).
Read-first: the only write path is the raw request() escape hatch,
which stays pinned to api.github.com.

Token connector: a fine-grained Personal Access Token, never in guest
code — every request names `credential: {ref: "connector.key.github",
prefix: "Bearer "}` and the host injects the Authorization header
after recording (anybao ADR-008 §1). Wraps the REST API at
https://api.github.com with a shared _fetch helper that sets the
required headers, follows the Link header for pagination, and backs
off on rate-limit 403/429 (honoring Retry-After via the sleep effect).
Every method returns a consistent {ok, ...} / {ok: False, error} shape
and trims GitHub's huge payloads down to small objects so the agent's
context isn't flooded. Trim, don't rename: kept fields carry the API's
own names (html_url, updated_at, user.login, …) so the model's
knowledge of the GitHub API transfers; the only invented keys are
derived values with no upstream scalar (repo, is_pr, text, kind).
"""

import base64
import json

_BASE = "https://api.github.com"
_TOKEN_URL = "https://github.com/settings/tokens?type=beta"
_CRED = {"ref": "connector.key.github", "header": "Authorization", "prefix": "Bearer "}
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "anybao-github@v1",
}
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_NOT_CONNECTED = (
    "GitHub not connected — create a fine-grained Personal Access Token at "
    + _TOKEN_URL + " with Read-only permissions (Repository: Issues, Pull "
    + "requests, Contents, Metadata; Account: Notifications for "
    + "list_notifications), then set GITHUB_TOKEN in the runtime environment "
    + "once (serve seeds the device-local secret store on start) or write a "
    + "localValue for connector.key.github on the config object."
)


def _clamp(n, default, cap):
    if not isinstance(n, (int, float)) or n <= 0:
        return default
    return min(int(n), cap)


def _trim(text, max_len):
    if not isinstance(text, str):
        return ""
    return text if len(text) <= max_len else text[:max_len] + "\n…[truncated]"


def _rate_limited(resp):
    return resp.status == 429 or (
        resp.status == 403 and resp.headers.get("x-ratelimit-remaining") == "0")


def _error_from(resp):
    """Non-ok response → {ok: False, error, status}. 401 = bad/expired
    token; rate-limit 403/429 = throttled; other 403 = a missing PAT
    permission or org SSO approval."""
    if resp.status == 401:
        return {"ok": False, "status": 401,
                "error": f"GitHub token invalid or expired — create a fresh fine-grained "
                         f"PAT at {_TOKEN_URL} and re-seed connector.key.github."}
    if _rate_limited(resp):
        return {"ok": False, "status": resp.status,
                "error": "GitHub rate limit hit — back off and retry later "
                         "(search is limited to 30/min; other reads 5000/hr)."}
    try:
        api_msg = resp.json().get("message") or f"HTTP {resp.status}"
    except (ValueError, AttributeError):
        api_msg = f"HTTP {resp.status}"
    if resp.status == 403:
        return {"ok": False, "status": 403,
                "error": f"GitHub returned 403 (likely a missing PAT permission "
                         f"or org SSO approval): {api_msg}"}
    return {"ok": False, "status": resp.status, "error": f"GitHub error: {api_msg}"}


def _fetch(path, params=None, method="get", body=None):
    """One request with bounded rate-limit backoff → the Response, or a
    {ok: False, ...} error dict. `path` may be a full url (pagination
    Link targets carry their own query string)."""
    url = path if path.startswith("http") else _BASE + path
    kw = {"headers": _HEADERS, "timeout": _TIMEOUT_S, "credential": _CRED}
    if params:
        kw["params"] = {k: v for k, v in params.items() if v not in (None, "")}
    if body is not None:
        kw["json"] = body
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = getattr(http, method)(url, **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        if resp.status < 300:
            return resp
        if _rate_limited(resp) and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            wait = min(max(wait, attempt + 1), 60)
            try:  # noqa: SIM105 - contextlib is outside the guest allowlist
                effect("sleep", {"seconds": wait})  # noqa: F821 - guest global
            except EffectError:  # noqa: F821 - a failed sleep just retries sooner
                pass
            continue
        return _error_from(resp)
    return {"ok": False, "error": f"GitHub rate limit — gave up after {_MAX_RETRIES} retries."}


def _is_err(x):
    return isinstance(x, dict) and x.get("ok") is False


def _json_of(resp):
    """Parsed body of a 2xx response, or {ok: False, ...} — GitHub
    occasionally hands back non-JSON (proxy/HTML error pages), which
    must surface as an error dict, never a guest traceback."""
    try:
        return resp.json()
    except ValueError:
        return {"ok": False, "status": resp.status,
                "error": "GitHub returned a non-JSON body"}


def _next_link(resp):
    """The Link header's rel="next" url, or None."""
    link = resp.headers.get("link")
    if not link:
        return None
    for seg in link.split(","):
        if 'rel="next"' in seg:
            lt, gt = seg.find("<"), seg.find(">")
            if 0 <= lt < gt:
                return seg[lt + 1:gt]
    return None


def _paged(path, params, max_items):
    """Follow Link rel="next" accumulating array bodies up to max_items."""
    out = []
    resp = _fetch(path, params)
    for _ in range(20):
        if _is_err(resp):
            return resp
        arr = _json_of(resp)
        if _is_err(arr):
            return arr
        if not isinstance(arr, list):
            return {"ok": False, "error": "unexpected GitHub response (not a list)"}
        out.extend(arr[:max_items - len(out)])
        url = _next_link(resp)
        if not url or len(out) >= max_items:
            break
        resp = _fetch(url)
    return {"ok": True, "items": out}


def _repo_full_name(item):
    repo = item.get("repository") or {}
    if repo.get("full_name"):
        return repo["full_name"]
    if item.get("repository_url"):
        return item["repository_url"].replace(_BASE + "/repos/", "")
    return None


def _issue(item):
    """Trim a GitHub issue/PR payload to a small subset — kept keys
    carry the API's own names (nested objects trimmed in place);
    `repo` and `is_pr` are derived (no scalar equivalent upstream)."""
    labels = [(lbl if isinstance(lbl, str) else (lbl or {}).get("name"))
              for lbl in (item.get("labels") or [])]
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "html_url": item.get("html_url"),
        "repository_url": item.get("repository_url"),
        "repo": _repo_full_name(item),
        "user": {"login": (item.get("user") or {}).get("login")},
        "labels": labels,
        "is_pr": bool(item.get("pull_request")),
        "updated_at": item.get("updated_at"),
        "body": _trim(item.get("body"), 2000),
    }


def _comment(c):
    return {"user": {"login": (c.get("user") or {}).get("login")},
            "created_at": c.get("created_at"),
            "body": _trim(c.get("body"), 1500)}


def _pr(item):
    """Trim a pulls-API payload — API names kept; `repo` derived."""
    base = item.get("base") or {}
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "merged_at": item.get("merged_at"),
        "draft": bool(item.get("draft")),
        "html_url": item.get("html_url"),
        "repo": (base.get("repo") or {}).get("full_name"),
        "user": {"login": (item.get("user") or {}).get("login")},
        "head": {"ref": (item.get("head") or {}).get("ref")},
        "base": {"ref": base.get("ref")},
        "updated_at": item.get("updated_at"),
        "body": _trim(item.get("body"), 2000),
    }


def _commit(c):
    commit = c.get("commit") or {}
    return {
        "sha": (c.get("sha") or "")[:12],
        "commit": {
            "message": _trim((commit.get("message") or "").split("\n")[0], 200),
            "author": {"name": (commit.get("author") or {}).get("name"),
                       "date": (commit.get("author") or {}).get("date")},
        },
        "author": {"login": (c.get("author") or {}).get("login")},
        "html_url": c.get("html_url"),
    }


def _repo(r):
    return {
        "full_name": r.get("full_name"),
        "private": r.get("private"),
        "fork": r.get("fork"),
        "archived": r.get("archived"),
        "description": _trim(r.get("description"), 300),
        "default_branch": r.get("default_branch"),
        "language": r.get("language"),
        "stargazers_count": r.get("stargazers_count"),
        "open_issues_count": r.get("open_issues_count"),
        "created_at": r.get("created_at"),
        "pushed_at": r.get("pushed_at"),
        "html_url": r.get("html_url"),
    }


def _b64text(content):
    # GitHub returns base64 with embedded newlines.
    clean = (content or "").replace("\n", "").replace("\r", "")
    try:
        return base64.b64decode(clean).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


@span("github.whoami", kind="getter")  # noqa: F821 - guest global
def whoami():
    """Verify the stored token; returns the authenticated user."""
    resp = _fetch("/user")
    if _is_err(resp):
        return resp
    u = _json_of(resp)
    if _is_err(u):
        return u
    return {"ok": True, "login": u.get("login"), "id": u.get("id"),
            "name": u.get("name"), "html_url": u.get("html_url")}


@span("github.list_my_issues", kind="getter")  # noqa: F821 - guest global
def list_my_issues(filter=None, state=None, since=None, per_page=None):
    """Issues + PRs assigned to / created by / mentioning the user."""
    per_page = _clamp(per_page, 30, 100)
    res = _paged("/issues", {
        "filter": filter or "assigned", "state": state or "open",
        "since": since, "per_page": per_page,
        "sort": "updated", "direction": "desc"}, per_page)
    if _is_err(res):
        return res
    return {"ok": True, "items": [_issue(it) for it in res["items"]]}


@span("github.list_repo_issues", kind="getter")  # noqa: F821 - guest global
def list_repo_issues(owner, repo, state=None, since=None, per_page=None):
    """One repo's issues + PRs, most recently updated first."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    per_page = _clamp(per_page, 30, 100)
    res = _paged(f"/repos/{owner}/{repo}/issues", {
        "state": state or "open", "since": since, "per_page": per_page,
        "sort": "updated", "direction": "desc"}, per_page)
    if _is_err(res):
        return res
    return {"ok": True, "items": [_issue(it) for it in res["items"]]}


@span("github.search_issues", kind="getter")  # noqa: F821 - guest global
def search_issues(q, per_page=None):
    """Targeted issue/PR search (GitHub search syntax). Budget is
    30 requests/min — use sparingly."""
    if not q:
        return {"ok": False, "error": "q (search query) is required"}
    resp = _fetch("/search/issues", {"q": q, "per_page": _clamp(per_page, 30, 100)})
    if _is_err(resp):
        return resp
    data = _json_of(resp)
    if _is_err(data):
        return data
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"ok": True, "totalCount": data.get("total_count"),
            "incompleteResults": data.get("incomplete_results"),
            "items": [_issue(it) for it in items]}


@span("github.get_issue", kind="getter")  # noqa: F821 - guest global
def get_issue(owner, repo, number):
    """One issue/PR with its comments."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    if number is None:
        return {"ok": False, "error": "number is required"}
    base = f"/repos/{owner}/{repo}/issues/{number}"
    resp = _fetch(base)
    if _is_err(resp):
        return resp
    payload = _json_of(resp)
    if _is_err(payload):
        return payload
    issue = _issue(payload)
    c_res = _paged(base + "/comments", {"per_page": 100}, 100)
    comments = [] if _is_err(c_res) else [_comment(c) for c in c_res["items"]]
    return {"ok": True, "issue": issue, "comments": comments}


@span("github.list_notifications", kind="getter")  # noqa: F821 - guest global
def list_notifications(all=False, since=None, per_page=None):
    """The authenticated user's notification inbox (unread by default;
    all=True includes read)."""
    per_page = _clamp(per_page, 30, 100)
    res = _paged("/notifications", {
        "all": "true" if all else "false", "since": since,
        "per_page": per_page}, per_page)
    if _is_err(res):
        return res
    items = [{"id": n.get("id"), "reason": n.get("reason"),
              "subject": {"title": (n.get("subject") or {}).get("title"),
                          "type": (n.get("subject") or {}).get("type"),
                          "url": (n.get("subject") or {}).get("url")},
              "repository": {
                  "full_name": (n.get("repository") or {}).get("full_name")},
              "updated_at": n.get("updated_at"), "unread": n.get("unread")}
             for n in res["items"]]
    return {"ok": True, "items": items}


@span("github.list_pull_requests", kind="getter")  # noqa: F821 - guest global
def list_pull_requests(owner, repo, state=None, base=None, per_page=None):
    """One repo's pull requests proper (branch refs, draft/merged),
    most recently updated first."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    per_page = _clamp(per_page, 30, 100)
    res = _paged(f"/repos/{owner}/{repo}/pulls", {
        "state": state or "open", "base": base, "per_page": per_page,
        "sort": "updated", "direction": "desc"}, per_page)
    if _is_err(res):
        return res
    return {"ok": True, "items": [_pr(it) for it in res["items"]]}


@span("github.get_pull_request", kind="getter")  # noqa: F821 - guest global
def get_pull_request(owner, repo, number):
    """One PR: detail (mergeable, diff stats) + changed files + review
    verdicts. Conversation comments live on get_issue with the same
    number."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    if number is None:
        return {"ok": False, "error": "number is required"}
    base = f"/repos/{owner}/{repo}/pulls/{number}"
    resp = _fetch(base)
    if _is_err(resp):
        return resp
    d = _json_of(resp)
    if _is_err(d):
        return d
    pr = _pr(d)
    pr.update({"additions": d.get("additions"), "deletions": d.get("deletions"),
               "changed_files": d.get("changed_files"),
               "commits": d.get("commits"), "mergeable": d.get("mergeable"),
               "merged_by": {"login": (d.get("merged_by") or {}).get("login")}})
    f_res = _paged(base + "/files", {"per_page": 100}, 100)
    files = [] if _is_err(f_res) else [
        {"filename": f.get("filename"), "status": f.get("status"),
         "additions": f.get("additions"), "deletions": f.get("deletions")}
        for f in f_res["items"]]
    r_res = _paged(base + "/reviews", {"per_page": 50}, 50)
    reviews = [] if _is_err(r_res) else [
        {"user": {"login": (r.get("user") or {}).get("login")},
         "state": r.get("state"), "submitted_at": r.get("submitted_at"),
         "body": _trim(r.get("body"), 1000)}
        for r in r_res["items"]]
    return {"ok": True, "pr": pr, "files": files, "reviews": reviews}


@span("github.list_commits", kind="getter")  # noqa: F821 - guest global
def list_commits(owner, repo, sha=None, path=None, since=None, per_page=None):
    """Recent commits, newest first. `sha` = branch/tag/sha to start
    from (default branch if omitted); `path` filters to one file/dir."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    per_page = _clamp(per_page, 30, 100)
    res = _paged(f"/repos/{owner}/{repo}/commits", {
        "sha": sha, "path": path, "since": since, "per_page": per_page}, per_page)
    if _is_err(res):
        return res
    return {"ok": True, "items": [_commit(c) for c in res["items"]]}


@span("github.list_repos", kind="getter")  # noqa: F821 - guest global
def list_repos(affiliation=None, sort=None, per_page=None):
    """Repos the user owns / collaborates on. `sort`: pushed (default)
    | created | updated | full_name; `affiliation`: comma-set of
    owner|collaborator|organization_member (default all three)."""
    per_page = _clamp(per_page, 30, 100)
    res = _paged("/user/repos", {
        "sort": sort or "pushed", "direction": "desc",
        "affiliation": affiliation, "per_page": per_page}, per_page)
    if _is_err(res):
        return res
    return {"ok": True, "items": [_repo(r) for r in res["items"]]}


@span("github.get_repo", kind="getter")  # noqa: F821 - guest global
def get_repo(owner, repo):
    """One repo's metadata (description, default branch, language,
    topics, counts)."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    resp = _fetch(f"/repos/{owner}/{repo}")
    if _is_err(resp):
        return resp
    d = _json_of(resp)
    if _is_err(d):
        return d
    out = _repo(d)
    out.update({"ok": True, "topics": d.get("topics") or [],
                "forks_count": d.get("forks_count")})
    return out


@span("github.request", kind="mutator")  # noqa: F821 - guest global
def request(method, path, params=None, body=None):
    """Raw authorized GitHub API request — the escape hatch for
    endpoints the methods above don't wrap (including writes).
    `method`: get|post|put|patch|delete; `path` starts with "/" (pinned
    to api.github.com — the credential never goes anywhere else);
    `body` is sent as JSON. Returns {ok, status, data} (parsed JSON;
    stringified + trimmed if huge)."""
    m = (method or "").lower()
    if m not in ("get", "post", "put", "patch", "delete"):
        return {"ok": False, "error": "method must be get|post|put|patch|delete"}
    if not isinstance(path, str) or not (
            path.startswith("/") or path.startswith(_BASE + "/")):
        return {"ok": False,
                "error": "path must start with / (requests are pinned to api.github.com)"}
    resp = _fetch(path, params, method=m, body=body)
    if _is_err(resp):
        return resp
    if not resp.text:
        return {"ok": True, "status": resp.status, "data": None}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": True, "status": resp.status, "text": _trim(resp.text, 8000)}
    dumped = json.dumps(data)
    if len(dumped) > 20000:
        return {"ok": True, "status": resp.status, "text": _trim(dumped, 20000),
                "note": "response truncated — narrow with params or a more "
                        "specific endpoint"}
    return {"ok": True, "status": resp.status, "data": data}


@span("github.get_readme", kind="getter")  # noqa: F821 - guest global
def get_readme(owner, repo):
    """A repository's README, decoded to text (trimmed to 8000 chars)."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    resp = _fetch(f"/repos/{owner}/{repo}/readme")
    if _is_err(resp):
        return resp
    data = _json_of(resp)
    if _is_err(data):
        return data
    return {"ok": True, "path": data.get("path"),
            "text": _trim(_b64text(data.get("content")), 8000)}


@span("github.get_contents", kind="getter")  # noqa: F821 - guest global
def get_contents(owner, repo, path):
    """A file (decoded, trimmed to 8000 chars) or a directory listing."""
    if not owner or not repo:
        return {"ok": False, "error": "owner and repo are required"}
    if path is None:
        return {"ok": False, "error": "path is required"}
    resp = _fetch(f"/repos/{owner}/{repo}/contents/{path}")
    if _is_err(resp):
        return resp
    data = _json_of(resp)
    if _is_err(data):
        return data
    if isinstance(data, list):
        return {"ok": True, "kind": "dir",
                "entries": [{"name": e.get("name"), "path": e.get("path"),
                             "type": e.get("type")} for e in data]}
    return {"ok": True, "kind": "file", "path": data.get("path"),
            "text": _trim(_b64text(data.get("content")), 8000)}


def main(args):
    return whoami()
