"""programs/github@v1 through the REAL guest kernel (conftest wires
anybao's tests/kernelenv.py at this repo's programs/). Fixtures are
live GitHub API responses recorded 2026-07-28 from public repos
(zarkone/literally.el, anyproto/any-store PR #132). Pinned here: the
normalized output shapes, credential plumbing (ref only — never a
token), Link-header pagination, rate-limit backoff via the sleep
effect, and the error contract (401 / missing secret / non-JSON →
{ok: False, error}, never a traceback)."""

import json
from pathlib import Path

from connectorenv import connector_kernel

FIX = Path(__file__).parent / "fixtures" / "github"


def body(name):
    return (FIX / name).read_text()


def ok(text, status=200, headers=None):
    return {"status": status, "headers": headers or {}, "body": text, "url": ""}


class FakeGitHub:
    """Serves http.* + sleep effects from ordered (url-substring →
    response) routes — first match wins, so specific routes go first.
    A list value is a consumable sequence (retry/backoff scripts); an
    Exception value is raised host-side → guest EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.slept = []

    def __call__(self, name, payload):
        if name == "sleep":
            self.slept.append(payload["seconds"])
            return {"slept": payload["seconds"]}
        assert name.startswith("http."), f"unexpected effect {name}"
        self.requests.append({"verb": name[len("http."):], **payload})
        url = payload["url"]
        for pattern, resp in self.routes:
            if pattern in url:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {url}")


def load(routes):
    fake = FakeGitHub(routes)
    gh = connector_kernel(effect=fake).use("github@v1")
    return gh, fake


# ---- auth + request plumbing ------------------------------------------------

def test_whoami_and_credential_plumbing():
    gh, fake = load([("/user", ok(body("user.json")))])
    out = gh.whoami()
    assert out == {"ok": True, "login": "zarkone", "id": 1899654,
                   "name": "Anatolii Smolianinov",
                   "html_url": "https://github.com/zarkone"}
    req = fake.requests[0]
    assert req["verb"] == "get"
    # the secret never crosses as a header — only the named ref
    assert req["credential"] == {"ref": "connector.key.github",
                                 "header": "Authorization", "prefix": "Bearer "}
    assert "Authorization" not in req["headers"]
    assert req["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert req["headers"]["Accept"] == "application/vnd.github+json"


def test_missing_secret_maps_to_connect_help():
    gh, _ = load([("/user", RuntimeError(
        'no secret for credential ref "connector.key.github"'))])
    out = gh.whoami()
    assert out["ok"] is False
    assert "GitHub not connected" in out["error"]
    assert "GITHUB_TOKEN" in out["error"]


def test_401_maps_to_token_expired():
    gh, _ = load([("/user", ok('{"message": "Bad credentials"}', status=401))])
    out = gh.whoami()
    assert out == {"ok": False, "status": 401, "error": out["error"]}
    assert "invalid or expired" in out["error"]


def test_non_json_body_is_an_error_not_a_traceback():
    gh, _ = load([("/user", ok("<html>proxy error</html>"))])
    out = gh.whoami()
    assert out["ok"] is False
    assert "non-JSON" in out["error"]


def test_rate_limit_backoff_honors_retry_after():
    limited = ok("", status=403,
                 headers={"x-ratelimit-remaining": "0", "retry-after": "7"})
    gh, fake = load([("/user", [limited, ok(body("user.json"))])])
    out = gh.whoami()
    assert out["ok"] is True and out["login"] == "zarkone"
    assert fake.slept == [7]
    assert len(fake.requests) == 2


def test_rate_limit_gives_up_after_retries():
    limited = ok("", status=403, headers={"x-ratelimit-remaining": "0"})
    gh, fake = load([("/user", limited)])
    out = gh.whoami()
    assert out["ok"] is False and out["status"] == 403
    assert "rate limit" in out["error"]
    assert len(fake.requests) == 4  # 1 + _MAX_RETRIES
    assert fake.slept == [1, 2, 3]  # attempt-indexed floor, no retry-after


# ---- issues -----------------------------------------------------------------

def test_list_repo_issues_normalizes():
    gh, fake = load([
        ("/repos/zarkone/literally.el/issues", ok(body("issues.json")))])
    out = gh.list_repo_issues("zarkone", "literally.el", state="all")
    assert out["ok"] is True
    item = out["items"][0]
    assert item["number"] == 1
    assert item["title"] == "tweaks for terminal emulator"
    assert item["state"] == "closed"
    assert item["repo"] == "zarkone/literally.el"
    assert item["repository_url"] == \
        "https://api.github.com/repos/zarkone/literally.el"
    assert item["user"] == {"login": "zarkone"}
    assert item["is_pr"] is True
    assert item["labels"] == []
    p = fake.requests[0]["params"]
    assert p["state"] == "all" and p["sort"] == "updated"


def test_list_repo_issues_requires_owner_repo():
    gh, fake = load([])
    assert gh.list_repo_issues("", "x")["ok"] is False
    assert gh.list_repo_issues("x", None)["ok"] is False
    assert fake.requests == []


def test_get_issue_with_comments():
    gh, _ = load([
        ("/issues/132/comments", ok(body("issue_comments.json"))),
        ("/issues/132", ok(body("issue.json")))])
    out = gh.get_issue("anyproto", "any-store", 132)
    assert out["ok"] is True
    assert out["issue"]["number"] == 132
    assert out["issue"]["is_pr"] is True
    assert out["comments"] == []


def test_search_issues():
    gh, fake = load([("/search/issues", ok(body("search.json")))])
    out = gh.search_issues("repo:zarkone/literally.el")
    assert out["ok"] is True
    assert out["totalCount"] == 1
    assert out["items"][0]["number"] == 1
    assert fake.requests[0]["params"]["q"] == "repo:zarkone/literally.el"


# ---- pull requests ----------------------------------------------------------

def test_list_pull_requests():
    gh, fake = load([("/repos/anyproto/any-store/pulls", ok(body("pulls.json")))])
    out = gh.list_pull_requests("anyproto", "any-store", state="all")
    assert out["ok"] is True
    assert [it["number"] for it in out["items"]] == [148, 147, 146, 145, 144]
    for it in out["items"]:
        assert it["repo"] == "anyproto/any-store"
        assert "merged_at" in it
        assert isinstance(it["draft"], bool)
        assert it["head"]["ref"] and it["base"]["ref"]
    assert fake.requests[0]["params"]["state"] == "all"


def test_get_pull_request_detail_files_reviews():
    gh, _ = load([
        ("/pulls/132/files", ok(body("pull_files.json"))),
        ("/pulls/132/reviews", ok(body("pull_reviews.json"))),
        ("/pulls/132", ok(body("pull.json")))])
    out = gh.get_pull_request("anyproto", "any-store", 132)
    assert out["ok"] is True
    pr = out["pr"]
    assert pr["number"] == 132
    assert pr["title"] == "query: typed sentinel for unknown filter operator"
    assert pr["merged_at"] == "2026-07-16T11:18:42Z"
    assert pr["merged_by"] == {"login": "cheggaaa"}
    assert pr["head"]["ref"] == "syn-79-unknown-operator-sentinel"
    assert pr["base"]["ref"] == "btree-fts"
    assert (pr["additions"], pr["deletions"], pr["changed_files"]) == (63, 2, 3)
    assert [f["filename"] for f in out["files"]] == [
        "query/cond_parse.go", "query/cond_parse_test.go", "query/errors.go"]
    assert out["files"][2]["status"] == "added"
    assert out["reviews"] == [{"user": {"login": "cheggaaa"},
                               "state": "APPROVED",
                               "submitted_at": out["reviews"][0]["submitted_at"],
                               "body": ""}]


# ---- commits + repos --------------------------------------------------------

def test_list_commits():
    gh, _ = load([("/repos/zarkone/literally.el/commits", ok(body("commits.json")))])
    out = gh.list_commits("zarkone", "literally.el")
    assert out["ok"] is True
    assert len(out["items"]) == 5
    first = out["items"][0]
    assert first["sha"] == "b03a4051ca14"  # shortened to 12
    assert first["commit"]["message"] == "misc"  # first line only
    assert first["author"] == {"login": "zarkone"}
    assert first["commit"]["author"]["date"] == "2025-10-15T16:15:47Z"


def test_pagination_follows_link_header():
    commits = json.loads(body("commits.json"))
    page2_url = "https://api.github.com/repos/zarkone/literally.el/commits?page=2"
    gh, fake = load([
        ("page=2", ok(json.dumps(commits[2:]))),
        ("/commits", ok(json.dumps(commits[:2]),
                        headers={"link": f'<{page2_url}>; rel="next"'}))])
    out = gh.list_commits("zarkone", "literally.el")
    assert out["ok"] is True
    assert len(out["items"]) == 5
    assert fake.requests[1]["url"] == page2_url


def test_pagination_stops_at_per_page():
    commits = json.loads(body("commits.json"))
    gh, fake = load([
        ("/commits", ok(json.dumps(commits),
                        headers={"link": '<https://api.github.com/x?page=2>; '
                                         'rel="next"'}))])
    out = gh.list_commits("zarkone", "literally.el", per_page=3)
    assert len(out["items"]) == 3
    assert len(fake.requests) == 1  # budget reached — next page never fetched


def test_list_repos():
    gh, fake = load([("/user/repos", ok(body("repos.json")))])
    out = gh.list_repos()
    assert out["ok"] is True
    assert [r["full_name"] for r in out["items"]] == [
        "zarkone/rmk-totem", "zarkone/zilpzalp-zmk", "zarkone/polkadot-editor"]
    assert all("default_branch" in r and "pushed_at" in r and "created_at" in r
               for r in out["items"])
    assert fake.requests[0]["params"]["sort"] == "pushed"
    gh.list_repos(sort="created")
    assert fake.requests[1]["params"]["sort"] == "created"


def test_get_repo():
    gh, _ = load([("/repos/zarkone/literally.el", ok(body("repo.json")))])
    out = gh.get_repo("zarkone", "literally.el")
    assert out["ok"] is True
    assert out["full_name"] == "zarkone/literally.el"
    assert out["default_branch"] == "master"
    assert out["language"] == "Emacs Lisp"
    assert out["stargazers_count"] == 9 and out["forks_count"] == 0
    assert out["topics"] == []


# ---- contents ---------------------------------------------------------------

def test_get_readme_decodes_base64():
    gh, _ = load([("/readme", ok(body("readme.json")))])
    out = gh.get_readme("zarkone", "literally.el")
    assert out["ok"] is True
    assert out["path"] == "README.org"
    assert out["text"].startswith("#+TITLE: My Emacs config")


def test_get_contents_file_and_dir():
    gh, _ = load([
        ("/contents/init.el", ok(body("contents_file.json"))),
        ("/contents/", ok(body("contents_dir.json")))])
    f = gh.get_contents("zarkone", "literally.el", "init.el")
    assert f["ok"] is True and f["kind"] == "file"
    assert f["path"] == "init.el" and len(f["text"]) > 0
    d = gh.get_contents("zarkone", "literally.el", "")
    assert d["ok"] is True and d["kind"] == "dir"
    assert {"name": ".custom-vars", "path": ".custom-vars",
            "type": "file"} in d["entries"]


# ---- raw request escape hatch -----------------------------------------------

def test_request_get():
    gh, fake = load([("/rate_limit", ok('{"resources": {"core": {}}}'))])
    out = gh.request("get", "/rate_limit")
    assert out == {"ok": True, "status": 200, "data": {"resources": {"core": {}}}}
    assert fake.requests[0]["credential"]["ref"] == "connector.key.github"


def test_request_post_sends_json_body():
    gh, fake = load([("/comments", ok('{"id": 1}', status=201))])
    out = gh.request("post", "/repos/zarkone/literally.el/issues/1/comments",
                     body={"body": "hi"})
    assert out == {"ok": True, "status": 201, "data": {"id": 1}}
    req = fake.requests[0]
    assert req["verb"] == "post"
    assert req["json"] == {"body": "hi"}


def test_request_patch():
    gh, fake = load([("/issues/1", ok('{"state": "closed"}'))])
    out = gh.request("patch", "/repos/zarkone/literally.el/issues/1",
                     body={"state": "closed"})
    assert out["ok"] is True and out["data"] == {"state": "closed"}
    assert fake.requests[0]["verb"] == "patch"


def test_request_empty_body_is_none():
    gh, _ = load([("/x", ok("", status=204))])
    assert gh.request("delete", "/x") == {"ok": True, "status": 204, "data": None}


def test_request_rejects_bad_method_and_foreign_host():
    gh, fake = load([])
    assert gh.request("head", "/x")["ok"] is False
    out = gh.request("get", "https://evil.example/steal")
    assert out["ok"] is False and "pinned to api.github.com" in out["error"]
    assert gh.request("get", "no-slash")["ok"] is False
    assert fake.requests == []  # nothing crossed the boundary


def test_request_truncates_huge_json():
    huge = json.dumps({"items": ["x" * 100] * 400})
    gh, _ = load([("/big", ok(huge))])
    out = gh.request("get", "/big")
    assert out["ok"] is True
    assert "data" not in out and len(out["text"]) <= 20000 + len("\n…[truncated]")
    assert "truncated" in out["note"]
