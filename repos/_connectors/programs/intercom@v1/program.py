"""Read-only Intercom connector — conversations, contacts, articles.

Customer conversations (summaries via list, full transcripts via get,
filtered search via Intercom's query DSL), contacts/leads (list +
search), help-center articles. Ingestion only — no writes. Cursor
pagination throughout: responses carry `pages`, pass
`pages.next.starting_after` back as `starting_after`. Methods return
{ok, ...} or {ok: False, error}. US-hosted API base; EU/AU workspaces
are not yet supported."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The Access Token never enters the guest: every request names
# `credential: {ref: "connector.key.intercom", prefix: "Bearer "}`
# and the host injects the Authorization header after recording
# (anybao ADR-008 §1). Every call pins the REQUIRED
# `Intercom-Version: 2.15` header. Base https://api.intercom.io (US;
# EU/AU need an override — not yet configurable here). 429 honors
# Retry-After with bounded backoff.

import json

_BASE = "https://api.intercom.io"
_CRED = {"ref": "connector.key.intercom", "header": "Authorization", "prefix": "Bearer "}
_HEADERS = {"Accept": "application/json", "Intercom-Version": "2.15"}
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_NOT_CONNECTED = (
    "Intercom not connected — create an Access Token in the Developer Hub "
    "(Settings -> Developers -> your app -> Configure -> Authentication), "
    "then set INTERCOM_ACCESS_TOKEN in the runtime environment once (serve "
    "seeds the device-local secret store on start) or write a localValue "
    "for connector.key.intercom on the config object."
)


def _clamp_per_page(n):
    # Intercom per_page max is 150; None = server default.
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    return min(int(n), 150)


def _req(verb, path, params=None, body=None):
    """One call with the required headers and bounded 429/Retry-After
    backoff → {ok, body} | {ok: False, error, status?}."""
    kw = {"headers": _HEADERS, "timeout": _TIMEOUT_S, "credential": _CRED}
    if params:
        kw["params"] = {k: v for k, v in params.items() if v not in (None, "")}
    if body is not None:
        kw["json"] = body
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = getattr(http, verb)(_BASE + path, **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        if resp.status == 429 and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, 2), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            try:
                b = resp.json()
                msg = (json.dumps(b.get("errors")) if b.get("errors")
                       else json.dumps(b.get("error")) if b.get("error")
                       else f"HTTP {resp.status}")
            except ValueError:
                msg = f"HTTP {resp.status}"
            if resp.status == 401:
                msg += " — token invalid/expired; re-seed connector.key.intercom"
            return {"ok": False, "status": resp.status,
                    "error": f"Intercom {verb.upper()} {path} failed: {msg}"}
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "status": resp.status, "error": "unparseable Intercom response"}
    return {"ok": False, "error": f"Intercom rate limit — gave up after {_MAX_RETRIES} retries."}


def _search_body(query, per_page, starting_after):
    body = {"query": query}
    pagination = {}
    pp = _clamp_per_page(per_page)
    if pp is not None:
        pagination["per_page"] = pp
    if starting_after:
        pagination["starting_after"] = starting_after
    if pagination:
        body["pagination"] = pagination
    return body


@span("intercom.me", kind="getter")  # noqa: F821 - guest global
def me():
    """The authenticated app/admin context — cheapest connectivity check.

    Returns `{ok, me}`.
    """
    r = _req("get", "/me")
    return r if not r["ok"] else {"ok": True, "me": r["body"]}


@span("intercom.list_conversations", kind="getter")  # noqa: F821 - guest global
def list_conversations(open=None, sort=None, order=None, per_page=None,
                       starting_after=None):
    """Page conversation SUMMARIES (no parts), newest updated first.
    Cursor: pass nextCursor back as starting_after.

    Returns `{ok, conversations, pages}` — next page via
    `pages.next.starting_after`.
    """
    params = {"per_page": _clamp_per_page(per_page),
              "starting_after": starting_after, "sort": sort, "order": order}
    if open is not None:
        params["open"] = "true" if open else "false"
    r = _req("get", "/conversations", params)
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "conversations": b.get("conversations") or [],
            "pages": b.get("pages")}


@span("intercom.search_conversations", kind="getter")  # noqa: F821 - guest global
def search_conversations(query, per_page=None, starting_after=None):
    """Filtered conversation search via the Intercom query DSL.

    Queries are {field, operator, value} with AND/OR groups.

    Returns `{ok, conversations, pages}`.
    """
    if not query:
        return {"ok": False, "error": "query is required (Intercom search DSL: "
                                      "{field, operator, value})"}
    r = _req("post", "/conversations/search",
             body=_search_body(query, per_page, starting_after))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "conversations": b.get("conversations") or [],
            "pages": b.get("pages")}


@span("intercom.get_conversation", kind="getter")  # noqa: F821 - guest global
def get_conversation(id, plaintext=True):
    """One conversation WITH its message parts — the transcript.

    Intercom caps at the 500 most recent parts; plaintext=True
    renders bodies as plain text.

    Returns `{ok, conversation}`.
    """
    if not id:
        return {"ok": False, "error": "conversation id is required"}
    params = {"display_as": "plaintext"} if plaintext else None
    r = _req("get", f"/conversations/{id}", params)
    return r if not r["ok"] else {"ok": True, "conversation": r["body"]}


@span("intercom.list_contacts", kind="getter")  # noqa: F821 - guest global
def list_contacts(per_page=None, starting_after=None):
    """Page contacts and leads.

    Returns `{ok, data, pages}`.
    """
    r = _req("get", "/contacts", {"per_page": _clamp_per_page(per_page),
                                  "starting_after": starting_after})
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "data": b.get("data") or [], "pages": b.get("pages")}


@span("intercom.search_contacts", kind="getter")  # noqa: F821 - guest global
def search_contacts(query, per_page=None, starting_after=None):
    """Filtered contact search via the Intercom query DSL.

    E.g. by email or custom attribute.

    Returns `{ok, data, pages}`.
    """
    if not query:
        return {"ok": False, "error": "query is required (Intercom search DSL: "
                                      "{field, operator, value})"}
    r = _req("post", "/contacts/search",
             body=_search_body(query, per_page, starting_after))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "data": b.get("data") or [], "pages": b.get("pages")}


@span("intercom.list_articles", kind="getter")  # noqa: F821 - guest global
def list_articles(per_page=None, starting_after=None):
    """Page help-center articles.

    Returns `{ok, data, pages}`.
    """
    r = _req("get", "/articles", {"per_page": _clamp_per_page(per_page),
                                  "starting_after": starting_after})
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "data": b.get("data") or [], "pages": b.get("pages")}


def main(args):
    return me()
