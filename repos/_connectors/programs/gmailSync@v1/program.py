"""Gmail → space sync: chunked full sync, history ticks, clean_html.

One tick per invocation (ADR-012 §2): a bounded full-sync slice while
the backlog drains, then coalesced `history.list` increments. The
tick's real governor is fuel — it checkpoints and exits when the
budget runs low. Bodies land as clean_html markdown (§4); the raw
MIME stays in Gmail. Cron recipe: an agent_triggers record with kind
"cron", program "connectors:gmailSync@v1", args {"space", "q"?}.
"""

# ADR-012 is the contract; §-refs inline. gmail@v1 stays the thin API
# wrapper (listing/labels + credential); hydration is our own 25-part
# multipart batch (>~25 concurrent parts trips Gmail's per-user limit).

__any_tool__ = True  # agent-callable (ADR-010 §4)

import re

_gm = use("gmail@v1")  # noqa: F821 - `use` is the guest global; same repo
_any = use("agent:any@v1")  # noqa: F821 - cross-repo dep, alias-qualified (ADR-009)

_DEFAULT_Q = "newer_than:1y"  # §2: whole-history is opt-in
_SLICE = 200                  # §2: soft cap per tick (trace containment)
_HYDRATE_CHUNK = 25           # §2: >~25 concurrent parts → inner 429s
_FUEL_FLOOR = 4_000_000_000   # checkpoint when remaining drops below;
# checked at tick entry, between list pages, and per MESSAGE in the
# write loops — one message (fetch+clean+write) stays well under this,
# so a check failure can never strand a half-converted chunk. The
# budget is per RUN: a conversation calling sync_now shares its 50B
# across every turn (seen live: two runs died at the wall before the
# entry/per-message checks existed).
_HTML_CAP = 300_000           # pathological bodies; clean_html input cap
_BOUNDARY = "anybao_gmail_sync"

EMAIL_TYPE = {
    "name": "Email", "xKey": "email",
    "properties": [
        {"name": "gmail_id"}, {"name": "thread_id"},
        {"name": "from"}, {"name": "to"}, {"name": "subject"},
        {"name": "date"},
        {"name": "internal_date", "kind": "number"},
        {"name": "label_ids", "kind": "array"},
        {"name": "snippet"},
    ],
}
STATE_TYPE = {
    "name": "Sync state", "xKey": "sync_state",
    "properties": [
        {"name": "cursor"}, {"name": "page_token"},
        {"name": "synced_count", "kind": "number"},
        {"name": "chain_hop", "kind": "number"},
        {"name": "chain_failures", "kind": "number"},
        {"name": "chain_gen", "kind": "number"},
    ],
}
PROGRESS_TYPE = {   # the agent_progress protocol — any-ui renders these
    "name": "Agent progress", "xKey": "agent-progress",
    "properties": [
        {"name": "job"}, {"name": "label"}, {"name": "status"},
        {"name": "current", "kind": "number"}, {"name": "total", "kind": "number"},
        {"name": "detail"}, {"name": "startedAt", "kind": "number"},
        {"name": "updatedAt", "kind": "number"}, {"name": "error"},
        {"name": "program"},
    ],
}
_MAX_CHAIN_FAILURES = 5


# --- clean_html (§4) — five passes over bs4 + markdownify ------------------

_QUOTE_MARKERS = ["gmail_quote", "yahoo_quoted", "moz-cite-prefix",
                  "OutlookMessageHeader"]
_TRACKER_HOSTS = re.compile(
    r"(click|track|link|email|mailtrack|list-manage|sendgrid|mailchi|braze|"
    r"customeriomail|exacttarget|mandrillapp|awstrack|mailings?)\.", re.I)
# per-link tracking params (LinkedIn/Google/Mailchimp vocab) — stripped
# from EVERY href; a link that stays huge after stripping collapses to
# its text (nobody follows a 500-char url from a note)
_TRACKING_PARAMS = re.compile(
    r"[?&](utm_[a-z]+|lipi|midToken|midSig|trk|trkEmail|eid|otpToken|"
    r"gclid|fbclid|mc_[ce]id|refId|trackingId|origin|si)=[^&#]*", re.I)
_MAX_HREF = 300
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|max-height\s*:\s*0", re.I)
_HEX2 = re.compile(r"[0-9a-fA-F]{2}")


def _unquote(s):
    # percent-decode without urllib (outside the allowlist) — utf-8,
    # broken bytes replaced
    parts = s.split("%")
    if len(parts) == 1:
        return s
    buf = bytearray(parts[0].encode())
    for p in parts[1:]:
        if len(p) >= 2 and _HEX2.match(p[:2]):
            buf.append(int(p[:2], 16))
            buf += p[2:].encode()
        else:
            buf += ("%" + p).encode()
    return buf.decode("utf-8", "replace")


def _netloc(url):
    m = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]*)", url or "")
    return (m.group(1) if m else "").lower()


def _query_values(url):
    q = url.partition("?")[2].partition("#")[0]
    return [_unquote(kv.partition("=")[2].replace("+", " "))
            for kv in q.split("&") if "=" in kv]


def _unwrap_tracking(href):
    """Redirect-wrapper URL → its real destination when recoverable."""
    for v in _query_values(href):
        if v.startswith("http") and "." in v[:40]:
            return v.split("?")[0] if not _TRACKER_HOSTS.search(_netloc(v)) else v
    return href


@span("gmailSync.clean_html", kind="getter")  # noqa: F821 - guest global
def clean_html(html):
    """Email HTML → {markdown, signature} — the §4 named filter.

    Five passes: layout-table flattening, quoted-chain + preheader +
    tracking-pixel removal, link hygiene (tracker unwrap, utm strip),
    notification-footer trim, signature split (signatures are persona
    raw material, returned separately). Pure compute — fuel-priced at
    ~15M/KB of input."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify as _md

    soup = BeautifulSoup((html or "")[:_HTML_CAP], "html.parser")

    for tag in soup(["script", "style", "head", "title", "meta", "link"]):
        tag.decompose()
    for tag in soup.find_all(style=_HIDDEN_STYLE):
        tag.decompose()
    for marker in _QUOTE_MARKERS:
        for tag in soup.find_all(class_=re.compile(marker)):
            tag.decompose()
    for bq in soup.find_all("blockquote", attrs={"type": "cite"}):
        bq.decompose()
    for img in soup.find_all("img"):
        w, h = img.get("width") or "", img.get("height") or ""
        if str(w) in ("0", "1") or str(h) in ("0", "1") or not img.get("alt"):
            img.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            continue
        if _TRACKER_HOSTS.search(_netloc(href)):
            real = _unwrap_tracking(href)
            if real != href and not _TRACKER_HOSTS.search(_netloc(real)):
                href = real
            else:
                a.replace_with(a.get_text(" ", strip=True))  # untraceable → text
                continue
        href = _TRACKING_PARAMS.sub("", href).rstrip("?&")
        base, hash_, frag = href.partition("#")
        if "?" not in base and "&" in base:
            base = base.replace("&", "?", 1)   # first surviving param re-anchors
            href = base + hash_ + frag
        if len(href) > _MAX_HREF:
            a.replace_with(a.get_text(" ", strip=True))   # still huge → text
            continue
        a["href"] = href
    # an anchor whose whole content was a (stripped) image renders as an
    # empty [](…) / [[ artifact — unwrap it before conversion
    for a in soup.find_all("a"):
        if not a.get_text(strip=True):
            a.unwrap()
    # email tables are layout, not data — cells become blocks (§4 pass 1)
    for cell in soup.find_all(["td", "th"]):
        cell.name = "div"
    for scaffold in soup.find_all(["table", "tbody", "thead", "tfoot", "tr"]):
        scaffold.unwrap()

    text = _md(str(soup), heading_style="ATX", strip=["img"])
    # strong footer markers cut anywhere; weak ones only in the tail fifth
    m = re.search(r"^\s*(—\s*\n\s*)?(Reply to this email directly\b|"
                  r"You are receiving this (email )?because\b)", text, re.M)
    if m:
        text = text[:m.start()]
    tail = min(len(text) // 2, max(150, len(text) // 5))
    m = re.search(r"^\s*(Unsubscribe( from these emails)?\b|Abbestellen\b)",
                  text[tail:], re.M | re.I)
    if m:
        text = text[:tail + m.start()]
    sig = ""
    m = re.search(r"\n--\s*\n", text)
    if m:
        text, sig = text[:m.start()], text[m.end():]
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[ \t|]+$", "", text, flags=re.M)
    return {"markdown": text.strip(), "signature": sig.strip()}


# --- hydration — own 25-part batch with per-part retry (§2) ----------------

def _batch_get(ids, fmt):
    """messages.get over multipart batch → {id: raw}; missing after
    retries stay absent (inner 429s come back as absent parts)."""
    import json as _json

    raws, todo = {}, list(ids)
    for attempt in range(4):
        if not todo:
            break
        if attempt:
            effect("sleep", {"seconds": 2 ** attempt})  # noqa: F821 - guest global
        parts = []
        for i, mid in enumerate(todo):
            parts.append(
                f"--{_BOUNDARY}\r\n"
                f"Content-Type: application/http\r\n"
                f"Content-ID: <item{i}>\r\n\r\n"
                f"GET /gmail/v1/users/me/messages/{mid}?format={fmt}\r\n\r\n")
        body = "".join(parts) + f"--{_BOUNDARY}--\r\n"
        resp = http.post(  # noqa: F821 - guest global
            "https://gmail.googleapis.com/batch/gmail/v1",
            headers={"Content-Type": f"multipart/mixed; boundary={_BOUNDARY}"},
            body=body, timeout=120, credential=_gm._CRED)
        if resp.status >= 300:
            raise RuntimeError(f"batch HTTP {resp.status}: {resp.text[:200]}")
        rb = (resp.headers.get("content-type") or "").split("boundary=")[1].split(";")[0].strip()
        for chunk in resp.text.split("--" + rb):
            chunk = chunk.strip()
            if not chunk or chunk == "--":
                continue
            segs = chunk.split("\r\n\r\n", 2)
            if len(segs) < 3:
                segs = chunk.split("\n\n", 2)
            if len(segs) < 3:
                continue
            try:
                msg = _json.loads(segs[2])
            except ValueError:
                continue
            if msg.get("id"):
                raws[msg["id"]] = msg
        todo = [i for i in todo if i not in raws]
    return raws


def _trim_full(raw):
    """Raw messages.get(full) → header scalars + preferred HTML body."""
    h = _gm._headers_of(raw.get("payload"))
    html = _gm._find_part(raw.get("payload"), "text/html")
    text = _gm._find_part(raw.get("payload"), "text/plain")
    return {"id": raw.get("id"), "threadId": raw.get("threadId") or "",
            "labelIds": raw.get("labelIds") or [],
            "internalDate": int(raw.get("internalDate") or 0),
            "from": h["from"], "to": h["to"], "subject": h["subject"],
            "date": h["date"], "snippet": raw.get("snippet") or "",
            "html": html or "", "text": text or ""}


# --- state object (§2) — the ui-context dance until derived ----------------

def _ensure_state(space):
    """The sync_state object: last-modified wins, stale ones deleted
    (query-then-create races mint duplicates — a tick must never
    follow a stale cursor). Returns (objectId, state-dict)."""
    rows = _any.query_objects(space, filter={"any.types": "sync_state"},
                              limit=10)
    rows.sort(key=lambda r: r.get("modifiedAt") or 0, reverse=True)
    if rows:
        for r in rows[1:]:
            try:  # noqa: SIM105 - best-effort, no contextlib in guest
                _any.delete_object(space, r["id"])
            except _any.AnyError:
                pass
        keep = rows[0]
        return keep["id"], dict(keep.get("sync_state") or {})
    made = _any.create_object(space, {
        "types": ["sync_state"], "name": "gmail sync state",
        "initialProperties": {"sync_state": {
            "cursor": "", "page_token": "", "synced_count": 0}}})
    return made["objectId"], {"cursor": "", "page_token": "", "synced_count": 0}


def _checkpoint(space, state_id, cursor, page_token, synced_count):
    # §2: written only after the batch it covers has landed
    _any.update_object(space, state_id, {"sync_state": {
        "cursor": cursor or "", "page_token": page_token or "",
        "synced_count": synced_count}})


def _fuel_low():
    return effect("fuel.state", {})["remaining"] < _FUEL_FLOOR  # noqa: F821


def _existing_ids(space, gmail_ids):
    """§2 idempotency: which of these gmail_ids already have objects."""
    if not gmail_ids:
        return set()
    rows = _any.query_objects(space, filter={
        "email.gmail_id": {"$in": list(gmail_ids)}}, limit=len(gmail_ids))
    return {(r.get("email") or {}).get("gmail_id") for r in rows}


def _write_email(space, msg):
    """create + put_markdown (§3); non-atomic — caller idempotency-gates."""
    cleaned = clean_html(msg["html"]) if msg["html"] else {
        "markdown": (msg["text"] or "").strip(), "signature": ""}
    obj = _any.create_object(space, {
        "types": ["email"],
        "name": (msg["subject"] or "(no subject)")[:120],
        "initialProperties": {"email": {
            "gmail_id": msg["id"], "thread_id": msg["threadId"],
            "from": msg["from"], "to": msg["to"],
            "subject": msg["subject"] or "(no subject)",
            "date": msg["date"], "internal_date": msg["internalDate"],
            "label_ids": msg["labelIds"], "snippet": msg["snippet"]}}})
    if cleaned["markdown"]:
        _any.put_markdown(space, obj["objectId"], cleaned["markdown"])
    return obj["objectId"]


# --- the tick (§2) ---------------------------------------------------------

def _full_slice(space, state_id, state, q, cap):
    """One bounded slice of the full sync; checkpoints page_token."""
    out = {"mode": "full", "made": 0, "skipped": 0, "failed": 0, "done": False}
    cursor = state.get("cursor") or ""
    if not cursor:
        prof = _gm._get("/profile")
        if not prof.get("ok"):
            return {**out, "error": prof.get("error")}
        cursor = str(prof["body"].get("historyId") or "")  # BEFORE listing: no gap
    token = state.get("page_token") or None
    synced = int(state.get("synced_count") or 0)
    ids = []
    while len(ids) < cap and not _fuel_low():
        page = _gm.list_messages(q=q, max_results=min(100, cap - len(ids)),
                                 page_token=token)
        if not page.get("ok"):
            return {**out, "error": page.get("error")}
        ids += [m["id"] for m in page.get("messages") or []]
        token = page.get("nextPageToken")
        if not token:
            break
    for i in range(0, len(ids), _HYDRATE_CHUNK):
        if _fuel_low():
            # cooperative governor (§2): sync as much as fits, then
            # checkpoint — unprocessed ids re-list next tick and the
            # gmail_id check absorbs the overlap.
            out["fuelStop"] = True
            break
        chunk = ids[i:i + _HYDRATE_CHUNK]
        have = _existing_ids(space, chunk)
        fresh = [m for m in chunk if m not in have]
        out["skipped"] += len(chunk) - len(fresh)
        if fresh:
            raws = _batch_get(fresh, "full")
            for mid in fresh:
                if mid not in raws:
                    out["failed"] += 1
                    continue
                if _fuel_low():
                    out["fuelStop"] = True
                    break
                _write_email(space, _trim_full(raws[mid]))
                out["made"] += 1
        if out.get("fuelStop"):
            break
    out["done"] = token is None and not out.get("fuelStop")
    synced += out["made"]
    _checkpoint(space, state_id, cursor, "" if out["done"] else (token or ""),
                synced)
    out["syncedCount"] = synced
    out["cursor"] = cursor
    return out


def _scope_ids(q, cap=500):
    """Newest-first ids matching q — the membership oracle for
    incremental adds. history.list is scope-blind (it reports EVERY
    mailbox change, exclusions or not — seen live: a -from:linkedin.com
    scope synced a LinkedIn mail), and q is Gmail search syntax only
    Gmail can evaluate. New arrivals sit at the top of a scoped list,
    so one or two pages resolve them. None = oracle unavailable."""
    ids, token = set(), None
    while len(ids) < cap:
        page = _gm.list_messages(q=q, max_results=100, page_token=token)
        if not page.get("ok"):
            return None
        ids |= {m["id"] for m in page.get("messages") or []}
        token = page.get("nextPageToken")
        if not token:
            break
    return ids


def _coalesce(history):
    """History records → (added, labelChanged, deleted) id sets — always
    per message id across ALL records, never record-by-record (§2)."""
    added, labels, deleted = set(), set(), set()
    for rec in history or []:
        for m in rec.get("messagesAdded") or []:
            added.add(m["message"]["id"])
        for m in rec.get("messagesDeleted") or []:
            deleted.add(m["message"]["id"])
        for m in (rec.get("labelsAdded") or []) + (rec.get("labelsRemoved") or []):
            labels.add(m["message"]["id"])
    added -= deleted
    labels -= added | deleted
    return added, labels, deleted


def _incremental(space, state_id, state, q):
    """history.list from the cursor; coalesced apply; cursor advances."""
    out = {"mode": "incremental", "made": 0, "labels": 0, "deleted": 0,
           "skipped": 0, "failed": 0}
    cursor = state.get("cursor")
    history, new_cursor, token = [], cursor, None
    while True:
        qs = {"startHistoryId": cursor, "maxResults": 500}
        if token:
            qs["pageToken"] = token
        r = _gm._get("/history" + _gm._qs(qs))
        if not r.get("ok"):
            if r.get("status") == 404:
                # Gmail retains history ~a week — fall back to a scoped
                # re-list from scratch; idempotency absorbs the overlap.
                _checkpoint(space, state_id, "", "", int(state.get("synced_count") or 0))
                return {**out, "fallback": "cursor expired — reset to full re-list",
                        "done": False}
            return {**out, "error": r.get("error")}
        body = r["body"]
        history += body.get("history") or []
        new_cursor = str(body.get("historyId") or new_cursor)
        token = body.get("nextPageToken")
        if not token:
            break
    added, labels, deleted = _coalesce(history)

    for mid in deleted:
        rows = _any.query_objects(space, filter={"email.gmail_id": mid}, limit=1)
        if rows:
            _any.delete_object(space, rows[0]["id"])
            out["deleted"] += 1
    if added:
        scope = _scope_ids(q)
        if scope is None:
            # cursor NOT advanced — better to replay this window next
            # tick than to advance past adds we couldn't scope-check
            return {**out, "error": "scope check failed (list_messages)",
                    "done": False}
        out["outOfScope"] = len(added - scope)
        added &= scope
    if added:
        have = _existing_ids(space, added)
        fresh = [m for m in added if m not in have]
        out["skipped"] += len(added) - len(fresh)
        raws = _batch_get(fresh, "full")
        for mid in fresh:
            if mid not in raws:
                out["failed"] += 1
                continue
            if _fuel_low():
                # cursor NOT advanced: this tick checkpoints nothing
                # new, the next one replays the history window and the
                # gmail_id check skips what already landed
                out["fuelStop"] = True
                _checkpoint(space, state_id, cursor, "",
                            int(state.get("synced_count") or 0) + out["made"])
                out["done"] = False
                return out
            _write_email(space, _trim_full(raws[mid]))
            out["made"] += 1
    if labels:
        raws = _batch_get(list(labels), "minimal")
        for mid, raw in raws.items():
            rows = _any.query_objects(space, filter={"email.gmail_id": mid},
                                      limit=1)
            if rows:
                _any.update_object(space, rows[0]["id"], {"email": {
                    "label_ids": raw.get("labelIds") or []}})
                out["labels"] += 1

    synced = int(state.get("synced_count") or 0) + out["made"] - out["deleted"]
    _checkpoint(space, state_id, new_cursor, "", synced)
    out["cursor"] = new_cursor
    out["syncedCount"] = synced
    out["done"] = True
    return out


def _tick(space, q=None, cap=None):
    if _fuel_low():
        # a conversation run may arrive with its budget nearly spent —
        # refuse cleanly instead of trapping mid-write
        return {"mode": "none", "fuelStop": True, "made": 0, "done": False,
                "note": "not enough fuel left in this run for a sync tick — "
                        "run it as a cron tick / its own run, or pass a "
                        "small max_messages"}
    _any.create_type(space, EMAIL_TYPE)     # §5 provisioning contract:
    _any.create_type(space, STATE_TYPE)     # idempotent ensure-resolve
    state_id, state = _ensure_state(space)
    q = q or _DEFAULT_Q
    cap = int(cap or _SLICE)
    if state.get("cursor") and not state.get("page_token"):
        return _incremental(space, state_id, state, q)
    return _full_slice(space, state_id, state, q, cap)


# --- backfill chain (self-scheduling once-triggers) ------------------------
# Each hop is its OWN run with a fresh fuel budget; the next hop is
# armed BEFORE the work, so any death — even an uncatchable fuel trap —
# leaves the chain alive to resume from the last checkpoint. Fresh
# record id per hop (the scheduler consumes a once-shot before the run
# and rolls last_run_at into the record afterwards — re-arming the same
# id would race that); fired hops stay behind as the audit trail.

def _space_id(s):
    # spaceConfig → a plain id/name STRING. The agent legitimately
    # passes a list_spaces() row or baoSpaceConfig (start_backfill's
    # docstring says to) — but the chain stores this in trigger args
    # and slices it into trigger ids, so normalize once at the entry
    # (live 08-13: KeyError slice(8,16) building a hop id from a dict).
    if isinstance(s, dict):
        s = s.get("id") or s.get("spaceId") or ""
    if not s:
        raise ValueError("pass a space id/name string, a list_spaces() "
                         "row, or baoSpaceConfig")
    return s


def _tid_frag(space_id):
    # the multibase id minus its constant "bafyrei…" prefix keeps two
    # spaces' chains from colliding; short names pass through whole
    return space_id[8:16] or space_id


def _trigger_anchor(agent_space):
    """The agent-triggers anchor object — OLDEST wins, the identical
    rank the runtime's ensure_typed applies, so both sides converge."""
    rows = _any.query_objects(agent_space, filter={
        "any.name": "agent-triggers", "any.types": "agent_trigger"}, limit=10)
    if not rows:
        raise RuntimeError("no agent-triggers anchor in the agent space")
    rows.sort(key=lambda r: (r.get("createdAt") or 0, r.get("id") or ""))
    return rows[0]["id"]


def _arm_hop(agent_space, space, q, gen, hop):
    # gen (bumped on every start_backfill) keeps ids fresh across
    # re-arms: a fired once-trigger is consumed forever — the runner's
    # lastRunAt survives upserts (triggers.rs once_due) — so reusing an
    # id from an earlier chain arms a dead trigger while reporting
    # armed (live: 08-13, hop-6 id reuse left the re-armed chain
    # silently inert).
    tid = f"gmailSyncBackfill-{_tid_frag(space)}-g{gen}-h{hop}"
    _any.upsert_record(agent_space, _trigger_anchor(agent_space),
                       "agent_triggers", tid, {
        "kind": "once", "spec": {"at": now()},  # noqa: F821 - past `at` fires late
        "program": "connectors:gmailSync@v1",
        "args": {"space": space, "q": q, "chain": True,
                 "triggerSpace": agent_space, "gen": gen, "hop": hop},
        "enabled": True, "name": f"gmail backfill hop {hop}"})
    return tid


def _notify_agent(agent_space, space, gen, text):
    # Chain end (drained or breaker) pokes the agent, not the chat: a
    # once-trigger on toolcaller whose userText is a system nudge — the
    # agent reads the outcome and writes the chat update itself (a raw
    # chat_send here couldn't contextualize or advise). Best-effort:
    # the sync result must not fail on a notification hiccup.
    try:
        chat = _any.general_chat(agent_space)
        tid = f"gmailSyncNotify-{_tid_frag(space)}-g{gen}"
        _any.upsert_record(agent_space, _trigger_anchor(agent_space),
                           "agent_triggers", tid, {
            "kind": "once", "spec": {"at": now()},  # noqa: F821 - guest global
            "program": "agent:toolcaller@v1",
            "args": {"space": agent_space, "chatId": chat, "userText": text},
            "enabled": True, "name": "gmail backfill report"})
        return tid
    except Exception:
        return None


def _progress(space, job, fields):
    """One agent_progress object per job, properties rewritten per hop
    (per-tick updates ARE the throttle — never per message)."""
    rows = _any.query_objects(space, filter={"agent-progress.job": job}, limit=1)
    if rows:
        _any.update_object(space, rows[0]["id"], {"agent-progress": fields})
        return rows[0]["id"]
    made = _any.create_object(space, {
        "types": ["agent-progress"], "name": fields.get("label") or job,
        "initialProperties": {"agent-progress": {"job": job, **fields}}})
    return made["objectId"]


def _chain_hop(space, q, agent_space, hop, gen):
    state_id, state = _ensure_state(space)
    fails = int(state.get("chain_failures") or 0)
    if fails >= _MAX_CHAIN_FAILURES:
        # keep chain_hop truthful even on the breaker hop — a stale
        # chain_hop fed the 08-13 id-reuse bug
        _any.update_object(space, state_id, {"sync_state": {"chain_hop": hop}})
        _progress(space, "gmail-backfill", {
            "status": "failed", "detail": f"stalled after {fails} failed hops",
            "error": "chain circuit-breaker open — fix the cause, then "
                     "start_backfill again", "updatedAt": now()})  # noqa: F821
        notified = _notify_agent(agent_space, space, gen, (
            "[system nudge — automated, no user on this turn] The gmail "
            f"backfill chain for space '{space}' STOPPED: circuit breaker "
            f"open after {fails} consecutive failed hops. Check the "
            "gmail-backfill progress object and gmailSync.status for the "
            "cause, then reply with ONE short message telling the user what "
            "broke and what you propose to do. Your reply reaches the chat "
            "by itself — do NOT chat_send it, or it posts twice (once as "
            "the user)."))
        return {"mode": "chain", "error": f"circuit breaker open ({fails} "
                "consecutive failed hops)", "done": False, "notified": notified}
    backlog = not state.get("cursor") or bool(state.get("page_token"))
    if backlog:
        _arm_hop(agent_space, space, q, gen, hop + 1)
    # bump-early: a hop that dies pre-checkpoint still counts against
    # the breaker; a completed hop resets it below
    _any.update_object(space, state_id, {"sync_state": {
        "chain_hop": hop, "chain_failures": fails + 1}})
    out = _tick(space, q=q)
    ok = not out.get("error")
    _any.update_object(space, state_id, {"sync_state": {
        "chain_failures": 0 if ok else fails + 1}})
    done = ok and not backlog   # a hop entered in steady state ends the chain
    _progress(space, "gmail-backfill", {
        "status": "done" if done else ("running" if ok else "failed"),
        "current": int(out.get("syncedCount") or state.get("synced_count") or 0),
        "detail": f"hop {hop}: {out.get('mode')} made={out.get('made')}",
        "error": str(out.get("error") or ""), "updatedAt": now(),  # noqa: F821
        "program": "connectors:gmailSync@v1"})
    notified = None
    if done:
        notified = _notify_agent(agent_space, space, gen, (
            "[system nudge — automated, no user on this turn] The gmail "
            f"backfill for space '{space}' FINISHED: backlog drained, "
            f"{int(out.get('syncedCount') or state.get('synced_count') or 0)} "
            "messages synced in total. Verify with gmailSync.status, then "
            "reply with ONE short completion update (mention the email "
            "count and that incremental ticks take over from here). Your "
            "reply reaches the chat by itself — do NOT chat_send it, or it "
            "posts twice (once as the user)."))
    return {**out, "hop": hop, "gen": gen, "chainArmed": backlog,
            "chainDone": done, "notified": notified}


# --- agent/user surface ----------------------------------------------------

@span("gmailSync.sync_now", kind="mutator")  # noqa: F821 - guest global
def sync_now(space, q=None, max_messages=None):
    """Run one sync tick now → {mode, made, skipped, …, done, cursor}.

    First runs drain the backlog one bounded slice at a time (call
    again — or let the cron — until done: True); after that each call
    is a coalesced history increment. q narrows the scope with Gmail
    search syntax (default newer_than:1y; exclusions are negative
    terms like -from:x). FUEL: each synced message costs ~0.3-0.6B of
    the RUN's 50B budget, shared with every other turn of a
    conversation — from chat, pass max_messages (≤50) and call
    repeatedly, or better register the cron and let ticks run alone.
    The tick self-checkpoints on low fuel (fuelStop: True) — nothing
    is lost, the next call resumes."""
    return _tick(space, q=q, cap=max_messages)


@span("gmailSync.start_backfill", kind="mutator")  # noqa: F821 - guest global
def start_backfill(space, agent_space, q=None):
    """Drive the WHOLE initial sync unattended → {armed, estimatedTotal}.

    The reliable path for big backlogs: arms a self-chaining
    once-trigger — every hop is its own run with a fresh fuel budget,
    the next hop is armed before work starts (a crashed hop resumes
    from the checkpoint), and a circuit breaker stops the chain after
    5 consecutive failed hops. `agent_space` is the serving agent's
    space (baoSpaceConfig — triggers live on its anchor). Progress is
    an `agent-progress` object in the target space (job
    "gmail-backfill"); watch it or `status(space)`. When the chain
    ends — backlog drained OR breaker — it arms a toolcaller nudge so
    the agent reports the outcome into its chat. Idempotent: re-arming
    resumes where the checkpoint left off (each arm is a fresh chain
    generation with its own trigger ids)."""
    # accept any spaceConfig shape (id, name, row, baoSpaceConfig) but
    # chain on plain strings — trigger args and ids are built from them
    space, agent_space = _space_id(space), _space_id(agent_space)
    q = q or _DEFAULT_Q
    _any.create_type(space, EMAIL_TYPE)
    _any.create_type(space, STATE_TYPE)
    _any.create_type(space, PROGRESS_TYPE)
    state_id, state = _ensure_state(space)
    gen = int(state.get("chain_gen") or 0) + 1
    _any.update_object(space, state_id, {"sync_state": {
        "chain_failures": 0, "chain_gen": gen}})
    total, token = 0, None
    while total < 5000:            # bounded estimate; 0 stays honest
        page = _gm.list_messages(q=q, max_results=100, page_token=token)
        if not page.get("ok"):
            break
        total += len(page.get("messages") or [])
        token = page.get("nextPageToken")
        if not token:
            break
    hop = int(state.get("chain_hop") or 0) + 1
    _progress(space, "gmail-backfill", {
        "label": "Gmail backfill", "status": "running", "current":
        int(state.get("synced_count") or 0),
        "total": total if token is None else 0,   # token left ⇒ indeterminate
        "detail": "armed", "startedAt": now(), "updatedAt": now(),  # noqa: F821
        "error": "", "program": "connectors:gmailSync@v1"})
    trigger = _arm_hop(agent_space, space, q, gen, hop)
    return {"armed": True, "trigger": trigger,
            "estimatedTotal": total if token is None else None,
            "resumingFrom": int(state.get("synced_count") or 0)}


@span("gmailSync.status", kind="getter")  # noqa: F821 - guest global
def status(space):
    """Sync bookkeeping → {cursor, pageToken, syncedCount, emailCount}.

    emailCount is the live object count (exact up to 10k); a cursor
    with an empty pageToken means backlog drained, ticking
    incrementally."""
    types = {t.get("xKey") for t in _any.list_types(space)}
    if "sync_state" not in types:
        return {"configured": False, "emailCount": 0}
    rows = _any.query_objects(space, filter={"any.types": "sync_state"}, limit=10)
    rows.sort(key=lambda r: r.get("modifiedAt") or 0, reverse=True)
    st = dict(rows[0].get("sync_state") or {}) if rows else {}
    emails = _any.query_objects(space, filter={"any.types": "email"},
                                limit=10000) if "email" in types else []
    return {"configured": bool(rows), "cursor": st.get("cursor") or "",
            "pageToken": st.get("page_token") or "",
            "syncedCount": int(st.get("synced_count") or 0),
            "emailCount": len(emails)}


def main(args):
    # trigger entry (§1): cron args {"space", "q"?, "maxMessages"?};
    # backfill-chain hops add {"chain": True, "triggerSpace", "gen", "hop"}
    if args.get("chain"):
        return _chain_hop(args["space"], args.get("q") or _DEFAULT_Q,
                          args["triggerSpace"], int(args.get("hop") or 1),
                          int(args.get("gen") or 0))
    return _tick(args["space"], q=args.get("q"), cap=args.get("maxMessages"))
