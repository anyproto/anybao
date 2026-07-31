"""Read-only Gmail connector — search, messages, threads, labels.

Message stubs by Gmail query/labels, one message decoded (headers +
snippet + text/plain body — never the raw MIME tree), whole threads
in order, the label list, and a thin q: search wrapper. Auth rides
googleAuth@v1 (one consent covers the Google family) — not-connected
errors say to run googleAuth.connect(). Methods return {ok, ...} or
{ok: False, error}; page sizes capped (messages.get costs 20 quota
units each). Scope: gmail.readonly."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Tokens never enter guest code: every request names the managed
# `credential: {ref: "connector.oauth.google", ...}` (from
# googleAuth@v1) and the broker refreshes/injects host-side at
# request time (anybao ADR-011 §6). Trim, don't rename (C1): kept
# fields carry Gmail's own names (id, threadId, labelIds, snippet,
# internalDate); the derived keys are the decoded header/body scalars
# Gmail buries in the MIME tree (from, to, cc, subject, date, body).

import base64
import re

_auth = use("googleAuth@v1")  # noqa: F821 - guest global
_CRED = _auth._CRED

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_DEFAULT_MAX = 20
_HARD_MAX = 100
_MAX_RETRIES = 3
_TIMEOUT_S = 60
_BODY_CAP = 20_000

_AUTH_PREFIXES = ("not_connected", "oauth_reconsent_required",
                  "not_configured", "consent_timeout")


def _clamp(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return _DEFAULT_MAX
    return min(int(n), _HARD_MAX)


def _quote(s):
    """Percent-encode for a URL query value (no urllib in the kernel)."""
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(c if c in safe else "".join(f"%{b:02X}" for b in c.encode()) for c in str(s))


def _qs(params):
    """?k=v&k=v query string; list values repeat the key (labelIds);
    None/"" skipped. The broker's `params` kwarg can't repeat keys,
    hence guest-side assembly."""
    parts = []
    for k, v in params.items():
        if v is None or v == "" or v == []:
            continue
        for item in v if isinstance(v, list) else [v]:
            if item is None or item == "":
                continue
            parts.append(f"{_quote(k)}={_quote(item)}")
    return "?" + "&".join(parts) if parts else ""


def _get(path):
    """One authed GET with bounded 429 backoff → {ok, body} |
    {ok: False, error, status?}."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = http.get(_BASE + path, timeout=_TIMEOUT_S, credential=_CRED)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            msg = str(e)
            if msg.startswith(_AUTH_PREFIXES):
                return {"ok": False, "error": _auth._friendly(msg)}
            return {"ok": False, "error": f"request failed: {msg}"}
        if resp.status == 429 and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, 2 ** attempt), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            if resp.status == 429:
                return {"ok": False, "status": 429,
                        "error": f"Gmail rate limit — still throttled after "
                                 f"{_MAX_RETRIES} retries; back off and retry later."}
            if resp.status == 401:
                return {"ok": False, "status": 401,
                        "error": "Gmail rejected the token (HTTP 401) — run "
                                 "googleAuth.connect() in a user-facing turn to reauthorize."}
            if resp.status == 403:
                return {"ok": False, "status": 403,
                        "error": "Gmail denied access (HTTP 403) — the grant is likely "
                                 "missing the gmail.readonly scope; re-run "
                                 "googleAuth.connect() to extend consent."}
            try:
                api_msg = (resp.json().get("error") or {}).get("message") or f"HTTP {resp.status}"
            except (ValueError, AttributeError):
                api_msg = f"HTTP {resp.status}"
            return {"ok": False, "status": resp.status, "error": f"Gmail error: {api_msg}"}
        try:
            return {"ok": True, "body": resp.json()}
        except ValueError:
            return {"ok": False, "error": "Gmail returned a non-JSON body"}
    return {"ok": False, "error": "unreachable"}  # the last attempt returns above


def _b64url(data):
    """Gmail body data is URL-safe base64 without padding → utf-8 text."""
    if not data or not isinstance(data, str):
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _headers_of(payload):
    """{from,to,cc,subject,date} pulled from payload.headers (case-insensitive)."""
    out = {"from": "", "to": "", "cc": "", "subject": "", "date": ""}
    for h in (payload or {}).get("headers") or []:
        name = (h.get("name") or "").lower()
        if name in out:
            out[name] = h.get("value") or ""
    return out


def _find_part(payload, mime):
    """Depth-first first part of `mime` with body data → decoded text."""
    if not payload:
        return ""
    if payload.get("mimeType") == mime and (payload.get("body") or {}).get("data"):
        return _b64url(payload["body"]["data"])
    for part in payload.get("parts") or []:
        got = _find_part(part, mime)
        if got:
            return got
    return ""


def _strip_html(s):
    """Crude html→text fallback for html-only mail — lossy by design."""
    t = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", s or "",
               flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(ent, ch)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _extract_text(payload):
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain
    html = _find_part(payload, "text/html")
    return _strip_html(html) if html else ""


def _trim_message(raw):
    """Raw messages.get → the trimmed shape (the MIME tree is dropped)."""
    if not raw:
        return None
    h = _headers_of(raw.get("payload"))
    body = _extract_text(raw.get("payload"))
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP] + "\n…[truncated]"
    return {"id": raw.get("id"), "threadId": raw.get("threadId"),
            "labelIds": raw.get("labelIds") or [],
            "internalDate": raw.get("internalDate"),
            "from": h["from"], "to": h["to"], "cc": h["cc"],
            "subject": h["subject"], "date": h["date"],
            "snippet": raw.get("snippet") or "", "body": body}


@span("gmail.list_messages", kind="getter")  # noqa: F821 - guest global
def list_messages(q=None, label_ids=None, max_results=None, page_token=None):
    """Message stubs by Gmail query/labels → {ok, messages, nextPageToken, resultSizeEstimate}.

    messages are {id, threadId} stubs — fetch bodies one by one with
    get_message (20 quota units each; that's why pages cap at 100).
    q uses Gmail search syntax (from:, newer_than:7d, is:unread…)."""
    r = _get("/messages" + _qs({"q": q, "labelIds": label_ids,
                                "maxResults": _clamp(max_results),
                                "pageToken": page_token}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "messages": b.get("messages") or [],
            "nextPageToken": b.get("nextPageToken"),
            "resultSizeEstimate": b.get("resultSizeEstimate")}


@span("gmail.search", kind="getter")  # noqa: F821 - guest global
def search(query, label_ids=None, max_results=None, page_token=None):
    """Gmail q: search → {ok, messages, nextPageToken, resultSizeEstimate}.

    Same as list_messages with a required query, e.g.
    "from:boss newer_than:7d is:unread"."""
    if not query or not isinstance(query, str):
        return {"ok": False,
                "error": 'query is required (Gmail q: syntax, e.g. "newer_than:7d from:boss")'}
    return list_messages(q=query, label_ids=label_ids,
                         max_results=max_results, page_token=page_token)


@span("gmail.get_message", kind="getter")  # noqa: F821 - guest global
def get_message(id):
    """One message decoded → {ok, message} — headers + snippet + text body.

    message = {id, threadId, labelIds, internalDate, from, to, cc,
    subject, date, snippet, body} — the derived keys are the decoded
    header scalars and body; the raw MIME payload tree never returns.
    body is the first text/plain part (html-stripped fallback),
    capped at 20k chars; the raw MIME payload tree never returns."""
    if not id or not isinstance(id, str):
        return {"ok": False, "error": "id is required"}
    r = _get(f"/messages/{_quote(id)}" + _qs({"format": "full"}))
    if not r["ok"]:
        return r
    msg = _trim_message(r["body"])
    if not msg:
        return {"ok": False, "error": f"message not found: {id}"}
    return {"ok": True, "message": msg}


@span("gmail.get_thread", kind="getter")  # noqa: F821 - guest global
def get_thread(id):
    """Every message of a thread, in order → {ok, id, messages}.

    messages carry the same trimmed shape as get_message."""
    if not id or not isinstance(id, str):
        return {"ok": False, "error": "id is required"}
    r = _get(f"/threads/{_quote(id)}" + _qs({"format": "full"}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    msgs = [m for m in (_trim_message(raw) for raw in b.get("messages") or []) if m]
    return {"ok": True, "id": b.get("id") or id, "messages": msgs}


@span("gmail.list_labels", kind="getter")  # noqa: F821 - guest global
def list_labels():
    """The account's labels → {ok, labels: [{id, name, type}]}.

    No pagination — Gmail returns the full set; also the cheapest
    connectivity check."""
    r = _get("/labels")
    if not r["ok"]:
        return r
    labels = [{"id": lb.get("id"), "name": lb.get("name"), "type": lb.get("type")}
              for lb in (r["body"] or {}).get("labels") or []]
    return {"ok": True, "labels": labels}


def main(args):
    return list_labels()
