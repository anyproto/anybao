"""Gmail connector — search and read mail; write Bao's own drafts, never send.

Message stubs by query/labels, one message decoded (headers + text
body), whole threads, labels, the profile. Drafts: list/get, create
(plain text; reply_to=<message id> threads it as a reply), update and
delete Bao's own. Nothing sends: the user reviews and sends from Gmail,
and the runtime refuses any send (ADR-034). Auth rides googleAuth@v1:
reads need its default consent, drafts connect(writes=True). Methods
return {ok, ...} or {ok: False, error}. Mail already synced into a
space is read from its corpus: get_skill("gmailSync")."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Tokens never enter guest code: every request names the managed
# `credential: {ref: "connector.oauth.google", ...}` (from
# googleAuth@v1) and the broker refreshes/injects host-side at
# request time (anybao ADR-011 §6). Trim, don't rename (C1): kept
# fields carry Gmail's own names (id, threadId, labelIds, snippet,
# internalDate); the derived keys are the decoded header/body scalars
# Gmail buries in the MIME tree (from, to, cc, subject, date, body).

import base64
import email.policy
import email.utils
import re
from email.message import EmailMessage

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

# ADR-034 §4: the runtime lets a cell write only Bao-marked text/plain
# drafts, and change or delete only drafts that carry the marker
_DRAFT_MARKER = "X-Anybao-Draft"
_DRAFT_BODY_CAP = 100_000
_REFUSED = "credential_write_refused"
_REFUSED_HINT = (" — a hard limit of the runtime, not a transient error: Bao never "
                 "sends mail and changes only its own drafts. Do not retry or work "
                 "around it; tell the user what to do in Gmail.")
_ADDR = re.compile(r"[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+")
_REPLY_HEADERS = ("Message-ID", "References", "Subject", "From", "Reply-To",
                  "To", "Cc")


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
    return _request("get", path)


def _request(verb, path, body=None):
    """One authed request with bounded 429 backoff → {ok, body, status}
    | {ok: False, error, status?, refused?}. body goes as json= — the
    runtime's draft check reads it there (ADR-034 §3). A 204 is
    {ok, body: {}}."""
    write = verb != "get"
    send = getattr(http, verb)  # noqa: F821 - guest global
    kw = {"timeout": _TIMEOUT_S, "credential": _CRED}
    if body is not None:
        kw["json"] = body
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = send(_BASE + path, **kw)
        except EffectError as e:  # noqa: F821 - guest global
            msg = str(e)
            if msg.startswith(_AUTH_PREFIXES):
                return {"ok": False, "error": _auth._friendly(msg)}
            if msg.startswith(_REFUSED):
                return {"ok": False, "refused": True, "error": msg + _REFUSED_HINT}
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
            if resp.status == 403 and write:
                return {"ok": False, "status": 403,
                        "error": "Gmail denied the draft write (HTTP 403) — the grant "
                                 "likely lacks gmail.compose; ask the user, then run "
                                 "googleAuth.connect(writes=True) in a user-facing turn."}
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
        if resp.status == 204 or not (resp.text or "").strip():
            return {"ok": True, "body": {}, "status": resp.status}
        try:
            return {"ok": True, "body": resp.json(), "status": resp.status}
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
    """{from,to,cc,subject,date,list_unsubscribe,precedence} from
    payload.headers (case-insensitive). The last two are the bulk-mail
    signals (ADR-012 §5 noise gate) — dropping them left sender
    classification to address heuristics alone."""
    out = {"from": "", "to": "", "cc": "", "subject": "", "date": "",
           "list-unsubscribe": "", "precedence": ""}
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
            "list_unsubscribe": h["list-unsubscribe"],
            "precedence": h["precedence"],
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
    subject, date, list_unsubscribe, precedence, snippet, body} — the
    derived keys are the decoded header scalars and body;
    list_unsubscribe/precedence are the bulk-mail signals (empty for
    most human mail). body is the first text/plain part
    (html-stripped fallback), capped at 20k chars; the raw MIME
    payload tree never returns."""
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


@span("gmail.get_profile", kind="getter")  # noqa: F821 - guest global
def get_profile():
    """The mailbox's own address → {ok, emailAddress, messagesTotal, threadsTotal, historyId}."""
    r = _get("/profile")
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "emailAddress": b.get("emailAddress") or "",
            "messagesTotal": b.get("messagesTotal"),
            "threadsTotal": b.get("threadsTotal"),
            "historyId": b.get("historyId")}


# --- drafts (ADR-034 §4, §7) -------------------------------------------------

def _header(payload, name):
    """One header's value off a message payload, case-insensitive."""
    for h in (payload or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _clean(value, field):
    """A header value: str, no CR/LF (header injection), trimmed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} may not contain line breaks")
    return value.strip()


def _addresses(value, field):
    """str ("a@x.io, B <b@y.io>") or list → ["B <b@y.io>", …]; raises
    ValueError on anything that is not an address."""
    if value is None or value == "" or value == []:
        return []
    items = [value] if isinstance(value, str) else list(value)
    out = []
    for item in items:
        text = _clean(item, field)
        if not text:
            continue
        for name, addr in email.utils.getaddresses([text]):
            if not _ADDR.fullmatch(addr or ""):
                raise ValueError(f"{field}: {text!r} is not an email address")
            out.append(email.utils.formataddr((name, addr)) if name else addr)
    return out


def _unfold(value):
    """A header value from Gmail, folded or not → one line."""
    return " ".join((value or "").split())


def _addr_key(a):
    return email.utils.parseaddr(a)[1].lower()


def _dedupe(addrs, drop=()):
    seen, out = {k.lower() for k in drop if k}, []
    for a in addrs:
        k = _addr_key(a)
        if k and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _raw(to, cc, bcc, subject, body, in_reply_to="", references=""):
    """A Bao-marked text/plain UTF-8 message → base64url, the
    drafts.create `raw`. Python's email package folds headers and
    encodes non-ASCII as RFC 2047 words — the runtime wants an ASCII
    header block (ADR-034 §4)."""
    msg = EmailMessage(policy=email.policy.SMTP)
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg[_DRAFT_MARKER] = "1"
    msg.set_content(body, subtype="plain", charset="utf-8",
                    cte="quoted-printable")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")


def _check_body(body):
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("body must be a string (plain text)")
    if len(body) > _DRAFT_BODY_CAP:
        raise ValueError(f"body is over {_DRAFT_BODY_CAP} chars")
    return body


def _trim_draft(d):
    """drafts.* → {id, message: {id, threadId, labelIds}} (Gmail's names, C1)."""
    m = (d or {}).get("message") or {}
    return {"id": (d or {}).get("id"),
            "message": {"id": m.get("id"), "threadId": m.get("threadId"),
                        "labelIds": m.get("labelIds") or []}}


def _reply_context(reply_to, reply_all):
    """The original's threading facts → {threadId, inReplyTo, references,
    subject, to, cc} | {ok: False, error}."""
    q = _qs({"format": "metadata", "metadataHeaders": list(_REPLY_HEADERS)})
    r = _get(f"/messages/{_quote(reply_to)}" + q)
    if not r["ok"]:
        return r
    raw = r["body"] or {}
    p = raw.get("payload")
    mid = _unfold(_header(p, "Message-ID"))
    refs = " ".join(x for x in (_unfold(_header(p, "References")), mid) if x)
    to = _addresses(_header(p, "Reply-To") or _header(p, "From"), "reply_to.from")
    cc = []
    if reply_all:
        prof = _get("/profile")
        if not prof["ok"]:
            return prof
        own = (prof["body"] or {}).get("emailAddress") or ""
        to = _dedupe(to + _addresses(_header(p, "To"), "reply_to.to"), drop=[own])
        cc = _dedupe(_addresses(_header(p, "Cc"), "reply_to.cc"),
                     drop=[own] + [_addr_key(a) for a in to])
    return {"ok": True, "threadId": raw.get("threadId"), "inReplyTo": mid,
            "references": refs, "subject": _unfold(_header(p, "Subject")),
            "to": to, "cc": cc}


@span("gmail.list_drafts", kind="getter")  # noqa: F821 - guest global
def list_drafts(q=None, max_results=None, page_token=None):
    """The user's drafts → {ok, drafts, nextPageToken, resultSizeEstimate}.

    drafts are {id, message: {id, threadId}} stubs — read one with
    get_draft. q uses Gmail search syntax (e.g. "to:boss")."""
    r = _get("/drafts" + _qs({"q": q, "maxResults": _clamp(max_results),
                              "pageToken": page_token}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "drafts": b.get("drafts") or [],
            "nextPageToken": b.get("nextPageToken"),
            "resultSizeEstimate": b.get("resultSizeEstimate")}


@span("gmail.get_draft", kind="getter")  # noqa: F821 - guest global
def get_draft(draft_id):
    """One draft decoded → {ok, draft: {id, message, bao}}.

    message has get_message's shape plus bcc; bao=True when Bao wrote
    it (only those can be updated or deleted)."""
    if not draft_id or not isinstance(draft_id, str):
        return {"ok": False, "error": "draft_id is required"}
    r = _get(f"/drafts/{_quote(draft_id)}" + _qs({"format": "full"}))
    if not r["ok"]:
        return r
    d = r["body"] or {}
    raw = d.get("message") or {}
    msg = _trim_message(raw) or {}
    msg["bcc"] = _header(raw.get("payload"), "Bcc")
    bao = bool(_header(raw.get("payload"), _DRAFT_MARKER).strip())
    return {"ok": True, "draft": {"id": d.get("id") or draft_id, "message": msg,
                                  "bao": bao}}


@span("gmail.create_draft", kind="mutator")  # noqa: F821 - guest global
def create_draft(to=None, subject=None, body="", cc=None, bcc=None,
                 reply_to=None, reply_all=False):
    """Write a plain-text draft into the user's Gmail → {ok, draft: {id, message}}.

    draft.message is {id, threadId, labelIds}. Never sends: the user
    reviews it in Gmail Drafts and presses Send.
    to/cc/bcc: an address string ("a@x.io, B <b@y.io>") or a list.
    reply_to=<message id> threads it as a reply: threadId, In-Reply-To
    and References come from that message, subject defaults to
    "Re: <subject>", to to its Reply-To or From; reply_all=True adds
    its To and Cc minus the user. body is plain text (≤ 100k chars).
    Needs googleAuth.connect(writes=True) once."""
    try:
        to_l = _addresses(to, "to")
        cc_l = _addresses(cc, "cc")
        bcc_l = _addresses(bcc, "bcc")
        subj = _clean(subject, "subject")
        text = _check_body(body)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    thread_id, in_reply_to, references = None, "", ""
    if reply_to:
        if not isinstance(reply_to, str):
            return {"ok": False, "error": "reply_to must be a message id"}
        try:
            ctx = _reply_context(reply_to, bool(reply_all))
        except ValueError as e:
            return {"ok": False, "error": f"reply_to {reply_to}: {e}"}
        if not ctx["ok"]:
            return ctx
        thread_id, in_reply_to, references = (ctx["threadId"], ctx["inReplyTo"],
                                               ctx["references"])
        if not subj:
            orig = ctx["subject"].strip()
            subj = orig if orig.lower().startswith("re:") else f"Re: {orig}".strip()
        if not to_l:
            to_l = ctx["to"]
            cc_l = _dedupe(cc_l + ctx["cc"], drop=[_addr_key(a) for a in to_l])
    elif reply_all:
        return {"ok": False, "error": "reply_all needs reply_to=<message id>"}
    if not (to_l or cc_l or bcc_l):
        return {"ok": False, "error": "a draft needs at least one recipient (to=…), "
                                      "or reply_to=<message id>"}
    try:
        raw = _raw(to_l, cc_l, bcc_l, subj, text, in_reply_to, references)
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": f"could not build the message: {e}"}
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    r = _request("post", "/drafts", {"message": message})
    if not r["ok"]:
        return r
    return {"ok": True, "draft": _trim_draft(r["body"])}


@span("gmail.update_draft", kind="mutator")  # noqa: F821 - guest global
def update_draft(draft_id, to=None, subject=None, body=None, cc=None, bcc=None):
    """Replace one of Bao's drafts, keeping every field not given → {ok, draft}.

    Only drafts Bao created (get_draft → bao=True); a draft the user
    wrote or re-saved in Gmail is theirs — create a new one instead.
    Same argument shapes as create_draft; threading is kept."""
    if not draft_id or not isinstance(draft_id, str):
        return {"ok": False, "error": "draft_id is required"}
    r = _get(f"/drafts/{_quote(draft_id)}" + _qs({"format": "full"}))
    if not r["ok"]:
        return r
    old = (r["body"] or {}).get("message") or {}
    p = old.get("payload")
    if not _header(p, _DRAFT_MARKER).strip():
        return {"ok": False, "refused": True,
                "error": f"draft {draft_id} is the user's (no {_DRAFT_MARKER} header) "
                         "— Bao changes only its own drafts; create a new one instead"}
    try:
        to_l = _addresses(_header(p, "To") if to is None else to, "to")
        cc_l = _addresses(_header(p, "Cc") if cc is None else cc, "cc")
        bcc_l = _addresses(_header(p, "Bcc") if bcc is None else bcc, "bcc")
        subj = _clean(_unfold(_header(p, "Subject")) if subject is None else subject,
                      "subject")
        text = _check_body(_extract_text(p) if body is None else body)
        raw = _raw(to_l, cc_l, bcc_l, subj, text,
                   _unfold(_header(p, "In-Reply-To")),
                   _unfold(_header(p, "References")))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}
    message = {"raw": raw}
    if old.get("threadId"):
        message["threadId"] = old["threadId"]
    r = _request("put", f"/drafts/{_quote(draft_id)}",
                 {"id": draft_id, "message": message})
    if not r["ok"]:
        return r
    return {"ok": True, "draft": _trim_draft(r["body"])}


@span("gmail.delete_draft", kind="mutator")  # noqa: F821 - guest global
def delete_draft(draft_id):
    """Delete one of Bao's drafts for good → {ok, id, deleted}.

    Only drafts Bao created; the runtime refuses the user's own.
    deleted=False when it was already gone (sent or deleted)."""
    if not draft_id or not isinstance(draft_id, str):
        return {"ok": False, "error": "draft_id is required"}
    r = _request("delete", f"/drafts/{_quote(draft_id)}")
    if not r["ok"]:
        if r.get("refused") and "HTTP 404" in r["error"]:
            return {"ok": True, "id": draft_id, "deleted": False}
        return r
    return {"ok": True, "id": draft_id, "deleted": True}


def main(args):
    return list_labels()
