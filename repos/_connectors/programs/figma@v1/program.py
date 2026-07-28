"""Read-only Figma connector — file metadata, comments, text content.

Account identity, lightweight file metadata (cheap change checks),
threaded file comments (markdown), and a design file's extracted
TEXT-layer content — deliberately never the multi-megabyte raw node
tree. Figma has no "list all my files" API: pass a file key or a
pasted figma.com URL (file/design/board/proto links all parse).
Project/team listing needs the OAuth-only projects:read scope — a
plain PAT 403s there, and the error says so. Methods return {ok, ...}
or {ok: False, error}; 429 backoff built in."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The Personal Access Token never enters the guest: every request
# names `credential: {ref: "connector.key.figma", header:
# "X-Figma-Token"}` (Figma's own header, NOT Authorization/Bearer —
# that's OAuth-only); the host injects it after recording (anybao
# ADR-008 §1). Rate limits are tight and per-minute; 429 honors
# Retry-After with bounded backoff.

import re

_BASE = "https://api.figma.com/v1"
_TOKEN_URL = "https://www.figma.com/settings (Security -> Personal access tokens)"
_CRED = {"ref": "connector.key.figma", "header": "X-Figma-Token"}
_DEFAULT_DEPTH = 8
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_SCOPES = "current_user:read, file_metadata:read, file_content:read, file_comments:read"

_NOT_CONNECTED = (
    "Figma not connected — create a Personal Access Token at " + _TOKEN_URL
    + ", ticking read scopes (" + _SCOPES + "), then import an .env file "
    + "containing connector.key.figma=<token> (any-ui: Help > Import "
    + "connector keys; CLI: a .connectors.env beside anybao.toml)."
)


def _parse_file_key(s):
    """Accept a raw file key OR a pasted figma.com URL
    (https://www.figma.com/(file|design|board|proto)/:key/:name)."""
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"figma\.com/(?:file|design|board|proto)/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


def _get(path, params=None):
    """One GET with bounded 429/Retry-After backoff → {ok, body} |
    {ok: False, error, status?}. 401 = bad token, 403 = missing scope,
    404 = bad/inaccessible file key."""
    kw = {"timeout": _TIMEOUT_S, "credential": _CRED}
    if params:
        kw["params"] = {k: v for k, v in params.items() if v not in (None, "")}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = http.get(_BASE + path, **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        if resp.status == 429 and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, 2 ** attempt), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            if resp.status == 401:
                return {"ok": False, "status": 401,
                        "error": f"Figma rejected the token (HTTP 401). Regenerate at "
                                 f"{_TOKEN_URL} and re-seed connector.key.figma."}
            if resp.status == 403:
                return {"ok": False, "status": 403,
                        "error": f"Figma denied access (HTTP 403) — the token is likely "
                                 f"missing a required read scope. Regenerate the PAT at "
                                 f"{_TOKEN_URL} with {_SCOPES}."}
            if resp.status == 404:
                return {"ok": False, "status": 404,
                        "error": "Figma file/resource not found or inaccessible "
                                 "(HTTP 404) — check the file key/URL."}
            if resp.status == 429:
                return {"ok": False, "status": 429,
                        "error": f"Figma rate limit hit (HTTP 429) — backed off "
                                 f"{_MAX_RETRIES}x and gave up. Try again in a minute."}
            try:
                body = resp.json()
                msg = body.get("err") or body.get("message") or f"HTTP {resp.status}"
            except ValueError:
                msg = f"HTTP {resp.status}"
            return {"ok": False, "status": resp.status, "error": f"Figma: {msg}"}
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "status": resp.status, "error": "unparseable Figma response"}
    return {"ok": False, "error": f"Figma rate limit — gave up after {_MAX_RETRIES} retries."}


def _walk_text(node, page, out):
    """DFS the node tree collecting every TEXT layer's characters.
    Bounded by the depth-limited tree the API already returned."""
    if not isinstance(node, dict):
        return
    if node.get("type") == "CANVAS":
        page = node.get("name") or page
    chars = node.get("characters")
    if node.get("type") == "TEXT" and isinstance(chars, str) and chars:
        out.append({"nodeId": node.get("id"), "page": page or "",
                    "name": node.get("name") or "", "text": chars})
    for kid in node.get("children") or []:
        _walk_text(kid, page, out)


@span("figma.me", kind="getter")  # noqa: F821 - guest global
def me():
    """Current Figma account; doubles as the token validator."""
    r = _get("/me")
    if not r["ok"]:
        return r
    u = r["body"] or {}
    return {"ok": True, "id": u.get("id"), "handle": u.get("handle"),
            "email": u.get("email"), "imgUrl": u.get("img_url")}


@span("figma.get_file_meta", kind="getter")  # noqa: F821 - guest global
def get_file_meta(file_key):
    """Lightweight file metadata — a cheap has-it-changed check.

    Run it before pulling text. Accepts a key or figma.com URL."""
    key = _parse_file_key(file_key)
    if not key:
        return {"ok": False, "error": "file_key (or a figma.com file URL) is required"}
    r = _get(f"/files/{key}/meta")
    if not r["ok"]:
        return r
    f = (r["body"] or {}).get("file") or r["body"] or {}
    return {"ok": True, "fileKey": key, "name": f.get("name"),
            "lastModified": f.get("last_touched_at") or f.get("lastModified"),
            "editorType": f.get("editor_type") or f.get("editorType"),
            "thumbnailUrl": f.get("thumbnail_url") or f.get("thumbnailUrl")}


@span("figma.list_comments", kind="getter")  # noqa: F821 - guest global
def list_comments(file_key):
    """All comments on a file, markdown-rendered (threads via parentId)."""
    key = _parse_file_key(file_key)
    if not key:
        return {"ok": False, "error": "file_key (or a figma.com file URL) is required"}
    r = _get(f"/files/{key}/comments", {"as_md": "true"})
    if not r["ok"]:
        return r
    comments = [{"id": c.get("id"),
                 "author": (c.get("user") or {}).get("handle"),
                 "message": c.get("message"),
                 "createdAt": c.get("created_at"),
                 "resolvedAt": c.get("resolved_at"),
                 "parentId": c.get("parent_id")}
                for c in (r["body"] or {}).get("comments") or []]
    return {"ok": True, "fileKey": key, "count": len(comments), "comments": comments}


@span("figma.get_file_text", kind="getter")  # noqa: F821 - guest global
def get_file_text(file_key, depth=None):
    """Every TEXT layer's content; NEVER the raw node tree.

    Walks the depth-limited tree (default depth 8)."""
    key = _parse_file_key(file_key)
    if not key:
        return {"ok": False, "error": "file_key (or a figma.com file URL) is required"}
    depth = int(depth) if isinstance(depth, (int, float)) and depth > 0 else _DEFAULT_DEPTH
    r = _get(f"/files/{key}", {"depth": depth})
    if not r["ok"]:
        return r
    doc = r["body"] or {}
    out = []
    _walk_text(doc.get("document"), "", out)
    return {"ok": True, "fileKey": key, "name": doc.get("name"),
            "lastModified": doc.get("lastModified"), "depth": depth,
            "textNodes": out}


@span("figma.list_project_files", kind="getter")  # noqa: F821 - guest global
def list_project_files(project_id):
    """Files in a Figma project (needs the projects:read scope).

    That scope is private-OAuth-app-only — a plain PAT usually 403s
    here."""
    if not project_id or not isinstance(project_id, str):
        return {"ok": False, "error": "project_id is required"}
    r = _get(f"/projects/{project_id}/files")
    if not r["ok"]:
        return r
    files = (r["body"] or {}).get("files") or []
    return {"ok": True, "projectId": project_id, "name": (r["body"] or {}).get("name"),
            "count": len(files), "files": files}


@span("figma.list_team_projects", kind="getter")  # noqa: F821 - guest global
def list_team_projects(team_id):
    """Projects in a team (team id from a figma.com/team/:id/... URL).
    Same projects:read caveat as list_project_files."""
    if not team_id or not isinstance(team_id, str):
        return {"ok": False, "error": "team_id is required"}
    r = _get(f"/teams/{team_id}/projects")
    if not r["ok"]:
        return r
    projects = (r["body"] or {}).get("projects") or []
    return {"ok": True, "teamId": team_id, "name": (r["body"] or {}).get("name"),
            "count": len(projects), "projects": projects}


def main(args):
    return me()
