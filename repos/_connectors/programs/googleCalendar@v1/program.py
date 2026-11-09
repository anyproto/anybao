"""Google Calendar connector — read calendars and events; keep private holds.

Reads: the calendar list, events in a window (recurrences expanded),
one event, an upcoming view; sync_token in, nextSyncToken out. Writes:
create/update/delete Bao's own holds — private events on the primary
calendar with no guests; no one is notified. Bao cannot invite: to set
up a meeting, keep a hold and gmail.create_draft the invitation. Auth
rides googleAuth@v1: reads need its default consent, holds
connect(writes=True). Methods return {ok, ...} or {ok: False, error}."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Tokens never enter guest code: every request names the managed
# `credential: {ref: "connector.oauth.google", ...}` (from
# googleAuth@v1) and the broker refreshes/injects host-side at
# request time (anybao ADR-011 §6). Trim, don't rename (C1): kept
# fields carry Google Calendar's own names (summary, start/end,
# responseStatus, htmlLink, nextPageToken, nextSyncToken); the one
# derived key is conferenceLink — hangoutLink when set, else the
# first video entryPoint in conferenceData (upstream has no single
# scalar for "the meeting link").

import datetime
import hashlib
import random
import re

_auth = use("googleAuth@v1")  # noqa: F821 - guest global
_CRED = _auth._CRED
# ADR-034 §2: writes ride the separate drafts-and-holds handle
_CRED_WRITE = _auth._CRED_WRITE

_BASE = "https://www.googleapis.com/calendar/v3"
_DEFAULT_MAX = 50
_HARD_MAX = 250  # recurrence expansion can fan out fast
_MAX_RETRIES = 3
_TIMEOUT_S = 60

_AUTH_PREFIXES = ("not_connected", "oauth_reconsent_required",
                  "not_configured", "consent_timeout")

# ADR-034 §5: the runtime admits only private, guest-less holds on the
# primary calendar whose id starts with this prefix, sendUpdates=none
_HOLD_PREFIX = "baoagent"
_HOLD_PATH = "/calendars/primary/events"
_REFUSED = "credential_write_refused"
_REFUSED_HINT = (" — a hard limit of the runtime, not a transient error: Bao keeps "
                 "only its own private holds and never invites anyone. Do not retry "
                 "or work around it; tell the user what to do in Google Calendar.")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# RFC 3339 date-time; the offset is optional here (then time_zone names it)
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})?")
_TEXT_CAP = 8_000


def _clamp(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return _DEFAULT_MAX
    return min(int(n), _HARD_MAX)


def _quote(s):
    """Percent-encode for a URL query value (no urllib in the kernel)."""
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(c if c in safe else "".join(f"%{b:02X}" for b in c.encode()) for c in str(s))


def _qs(params):
    """?k=v&k=v query string; list values repeat the key;
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


def _write_auth_error(msg):
    """A write-handle failure → what to do (ADR-034 §6)."""
    if "no oauth provider" in msg:
        return ("this runtime predates the write guard (ADR-034) — Bao cannot write "
                "calendar holds until the app is updated")
    if msg.startswith(("not_connected", "oauth_reconsent_required")):
        return (msg + " — calendar holds need their own consent: ask the user, then run "
                "googleAuth.connect(writes=True) in a user-facing turn")
    return _auth._friendly(msg)


def _api_error(resp):
    """Google's error envelope → (message, reasons, status)."""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        return "", [], ""
    if not isinstance(err, dict):
        return "", [], ""
    reasons = [e.get("reason") or "" for e in err.get("errors") or [] if isinstance(e, dict)]
    return err.get("message") or "", reasons, err.get("status") or ""


def _rate_limited(resp):
    """Google also answers 403 for usage limits — those back off like a 429."""
    _, reasons, status = _api_error(resp)
    return status == "RESOURCE_EXHAUSTED" or any(
        r in ("rateLimitExceeded", "userRateLimitExceeded") for r in reasons)


def _scope_denied(resp):
    """A 403 that means the grant lacks the scope (or says nothing)."""
    message, reasons, _ = _api_error(resp)
    return (not message and not reasons) or "insufficientPermissions" in reasons \
        or "scope" in message.lower() or "insufficient" in message.lower()


def _request(verb, path, body=None):
    """One authed request with bounded 429 backoff → {ok, body, status}
    | {ok: False, error, status?, refused?}. body goes as json= (the
    runtime's hold check reads it there, ADR-034 §3); a 204 is
    {ok, body: {}}."""
    write = verb != "get"
    send = getattr(http, verb)  # noqa: F821 - guest global
    kw = {"timeout": _TIMEOUT_S, "credential": _CRED_WRITE if write else _CRED}
    if body is not None:
        kw["json"] = body
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = send(_BASE + path, **kw)
        except EffectError as e:  # noqa: F821 - guest global
            msg = str(e)
            if write and msg.startswith(_AUTH_PREFIXES):
                return {"ok": False, "error": _write_auth_error(msg)}
            if msg.startswith(_AUTH_PREFIXES):
                return {"ok": False, "error": _auth._friendly(msg)}
            if msg.startswith(_REFUSED):
                return {"ok": False, "refused": True, "error": msg + _REFUSED_HINT}
            return {"ok": False, "error": f"request failed: {msg}"}
        throttled = resp.status == 429 or (resp.status == 403 and _rate_limited(resp))
        if throttled and attempt < _MAX_RETRIES:
            try:
                wait = int(resp.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            effect("sleep", {"seconds": min(max(wait, 2 ** attempt), 60)})  # noqa: F821 - guest global
            continue
        if resp.status >= 300:
            if throttled:
                return {"ok": False, "status": resp.status,
                        "error": f"Calendar rate limit — still throttled after "
                                 f"{_MAX_RETRIES} retries; back off and retry later."}
            if resp.status == 401:
                return {"ok": False, "status": 401,
                        "error": "Calendar rejected the token (HTTP 401) — run "
                                 "googleAuth.connect() in a user-facing turn to reauthorize."}
            if resp.status == 403 and write and _scope_denied(resp):
                return {"ok": False, "status": 403,
                        "error": "Calendar denied the write (HTTP 403) — the grant likely "
                                 "lacks calendar.events; ask the user, then run "
                                 "googleAuth.connect(writes=True) in a user-facing turn."}
            if resp.status == 403:
                return {"ok": False, "status": 403,
                        "error": "Calendar denied access (HTTP 403) — the grant is likely "
                                 "missing the calendar.readonly scope; re-run "
                                 "googleAuth.connect() to extend consent."}
            try:
                api_msg = (resp.json().get("error") or {}).get("message") or f"HTTP {resp.status}"
            except (ValueError, AttributeError):
                api_msg = f"HTTP {resp.status}"
            return {"ok": False, "status": resp.status, "error": f"Calendar error: {api_msg}"}
        if resp.status == 204 or not (resp.text or "").strip():
            return {"ok": True, "body": {}, "status": resp.status}
        try:
            return {"ok": True, "body": resp.json(), "status": resp.status}
        except ValueError:
            return {"ok": False, "error": "Calendar returned a non-JSON body"}
    return {"ok": False, "error": f"Calendar rate limit — gave up after {_MAX_RETRIES} retries."}


def _trim_attendee(a):
    """email + responseStatus (+ displayName/organizer/self/optional
    only when set); the rest of Google's attendee object drops."""
    if not a:
        return None
    out = {"email": a.get("email") or "", "responseStatus": a.get("responseStatus") or ""}
    if a.get("displayName"):
        out["displayName"] = a["displayName"]
    if a.get("organizer"):
        out["organizer"] = True
    if a.get("self"):
        out["self"] = True
    if a.get("optional"):
        out["optional"] = True
    return out


def _conference_link(ev):
    """Best conferencing link: hangoutLink, else the first video
    entryPoint uri in conferenceData."""
    if ev.get("hangoutLink"):
        return ev["hangoutLink"]
    for ep in (ev.get("conferenceData") or {}).get("entryPoints") or []:
        if ep and ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return ""


def _trim_event(ev):
    """Raw Google event → the trimmed shape (field names verbatim, C1;
    conferenceLink is the one derived key)."""
    if not ev:
        return None
    attendees = [t for t in (_trim_attendee(a) for a in ev.get("attendees") or []) if t]
    out = {"id": ev.get("id"), "summary": ev.get("summary") or "",
           "status": ev.get("status") or "",
           "start": ev.get("start") or None, "end": ev.get("end") or None,
           "attendees": attendees,
           "location": ev.get("location") or "",
           "description": ev.get("description") or "",
           "organizer": ev.get("organizer") or None,
           "conferenceLink": _conference_link(ev),
           "htmlLink": ev.get("htmlLink") or "",
           "updated": ev.get("updated") or ""}
    if ev.get("recurringEventId"):
        out["recurringEventId"] = ev["recurringEventId"]
    return out


def _trim_events(items):
    return [t for t in (_trim_event(ev) for ev in items or []) if t]


@span("googleCalendar.list_calendars", kind="getter")  # noqa: F821 - guest global
def list_calendars(max_results=None, min_access_role=None, page_token=None):
    """The user's calendar list → {ok, calendars, nextPageToken}.

    calendars are {id, summary, primary, selected, hidden, accessRole, timeZone};
    selected and hidden default to False when omitted by Google.
    All returned calendars are kept; callers choose whether to filter.
    min_access_role filters (e.g. "writer"). Also the cheapest
    connectivity check. Pages cap at 250."""
    r = _get("/users/me/calendarList" + _qs({"maxResults": _clamp(max_results),
                                             "minAccessRole": min_access_role,
                                             "pageToken": page_token}))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    calendars = [{"id": c.get("id"), "summary": c.get("summary") or "",
                  "primary": bool(c.get("primary")),
                  "selected": bool(c.get("selected")),
                  "hidden": bool(c.get("hidden")),
                  "accessRole": c.get("accessRole") or "",
                  "timeZone": c.get("timeZone") or ""}
                 for c in b.get("items") or []]
    return {"ok": True, "calendars": calendars, "nextPageToken": b.get("nextPageToken")}


@span("googleCalendar.list_events", kind="getter")  # noqa: F821 - guest global
def list_events(calendar_id=None, time_min=None, time_max=None, q=None,
                max_results=None, page_token=None, sync_token=None):
    """Events in a time window → {ok, calendarId, events, nextPageToken, nextSyncToken, timeZone}.

    calendar_id defaults to "primary"; time_min/time_max are RFC3339.
    Recurrences expand (singleEvents, orderBy=startTime) UNLESS
    sync_token is given — Google rejects mixing syncToken with
    timeMin/timeMax/orderBy/q, so a sync call sends only the token +
    paging. events carry the trimmed shape of get_event; the derived
    key conferenceLink is hangoutLink or the first video entryPoint.
    Pages cap at 250 (default 50)."""
    calendar_id = calendar_id or "primary"
    if sync_token:
        params = {"syncToken": sync_token, "pageToken": page_token,
                  "maxResults": _clamp(max_results)}
    else:
        params = {"singleEvents": "true", "orderBy": "startTime",
                  "timeMin": time_min, "timeMax": time_max, "q": q,
                  "maxResults": _clamp(max_results), "pageToken": page_token}
    r = _get(f"/calendars/{_quote(calendar_id)}/events" + _qs(params))
    if not r["ok"]:
        return r
    b = r["body"] or {}
    return {"ok": True, "calendarId": calendar_id,
            "events": _trim_events(b.get("items")),
            "nextPageToken": b.get("nextPageToken"),
            "nextSyncToken": b.get("nextSyncToken"),
            "timeZone": b.get("timeZone") or ""}


@span("googleCalendar.get_event", kind="getter")  # noqa: F821 - guest global
def get_event(event_id, calendar_id=None):
    """One event in full → {ok, event}.

    event is {id, summary, status, start, end, attendees, location,
    description, organizer, conferenceLink, htmlLink, updated,
    recurringEventId?} — names verbatim from Google (C1) except the
    derived conferenceLink (hangoutLink, else the first video
    entryPoint uri). calendar_id defaults to "primary"."""
    if not event_id or not isinstance(event_id, str):
        return {"ok": False, "error": "event_id is required"}
    calendar_id = calendar_id or "primary"
    r = _get(f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}")
    if not r["ok"]:
        return r
    return {"ok": True, "event": _trim_event(r["body"])}


@span("googleCalendar.upcoming", kind="getter")  # noqa: F821 - guest global
def upcoming(max_results=None, calendar_id=None, page_token=None):
    """Events from now forward on the primary calendar → {ok, calendarId, events, nextPageToken, …}.

    Convenience over list_events(timeMin=now); same return shape.
    calendar_id overrides "primary"."""
    time_min = datetime.datetime.fromtimestamp(
        now(), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: F821 - guest global
    return list_events(calendar_id=calendar_id or "primary", time_min=time_min,
                       max_results=max_results, page_token=page_token)


# --- holds (ADR-034 §5, §7) -------------------------------------------------

def _text(value, field, required=False):
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > _TEXT_CAP:
        raise ValueError(f"{field} is over {_TEXT_CAP} chars")
    return value


def _when(value, field, time_zone):
    """"YYYY-MM-DD" → ({date}, date); an RFC 3339 date-time → ({dateTime,
    timeZone?}, comparable). A date-time without an offset needs
    time_zone (Google's rule)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required (RFC 3339 date-time or YYYY-MM-DD)")
    value = value.strip()
    if _DATE.fullmatch(value):
        try:
            return {"date": value}, ("date", datetime.date.fromisoformat(value))
        except ValueError:
            raise ValueError(f"{field}: {value!r} is not a calendar date") from None
    try:
        if not _DATETIME.fullmatch(value):
            raise ValueError
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field}: {value!r} is not an RFC 3339 date-time "
                         f"(e.g. 2026-11-10T15:00:00+01:00) or a YYYY-MM-DD date") from None
    out = {"dateTime": value}
    if time_zone:
        out["timeZone"] = time_zone
    if dt.tzinfo is None:
        if not time_zone:
            raise ValueError(f"{field}: {value!r} has no UTC offset — add one "
                             f"(…+01:00) or pass time_zone (e.g. \"Europe/Berlin\")")
        return out, ("local", dt)
    return out, ("abs", dt)


def _span(start, end, time_zone):
    """Validated {start}, {end} event times: same kind, end after start."""
    tz = _text(time_zone, "time_zone")
    s, (sk, sv) = _when(start, "start", tz)
    e, (ek, ev) = _when(end, "end", tz)
    if (sk == "date") != (ek == "date"):
        raise ValueError("start and end must both be dates (all day) or both date-times")
    if sk != "date" and sk != ek:
        raise ValueError("start and end must both carry a UTC offset, or neither")
    if not ev > sv:
        hint = (" — all-day ends are exclusive: a one-day hold on 2026-11-10 ends "
                "2026-11-11") if sk == "date" else ""
        raise ValueError(f"end must be after start{hint}")
    return s, e


def _hold_id(key, summary, start, end):
    """baoagent + 32 hex: from sha256(key) when keyed (a re-run finds
    the same hold), else from the inputs, now() and the run's seeded
    random stream (replay mints the same id, ADR-002 §4)."""
    seed = (f"key|{key}" if key is not None
            else f"{summary}|{start}|{end}|{now()}|{random.random()}")  # noqa: F821
    return _HOLD_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _own_id(event_id):
    if not isinstance(event_id, str) or not event_id.startswith(_HOLD_PREFIX):
        return {"ok": False, "refused": True,
                "error": f"event {event_id!r} is not one of Bao's holds (ids start with "
                         f"{_HOLD_PREFIX!r}) — Bao changes only its own; suggest the "
                         f"change to the user instead"}
    return None


@span("googleCalendar.create_event", kind="mutator")  # noqa: F821 - guest global
def create_event(summary, start, end, description=None, location=None,
                 time_zone=None, key=None):
    """Put a private hold on the user's primary calendar → {ok, event, created}.

    start/end: RFC 3339 date-times ("2026-11-10T15:00:00+01:00"; no
    offset needs time_zone, e.g. "Europe/Berlin") or YYYY-MM-DD dates
    for all day (end exclusive). event has get_event's shape. Nobody
    is invited or notified; Bao cannot add guests. key: a stable string
    (e.g. "followup:<thread id>") — a re-run with the same key returns
    the existing hold, created=False. Needs googleAuth.connect(writes=True)."""
    try:
        title = _text(summary, "summary", required=True)
        s, e = _span(start, end, time_zone)
        desc = _text(description, "description")
        loc = _text(location, "location")
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ValueError("key must be a non-empty string")
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    event_id = _hold_id(key, title, start, end)
    body = {"id": event_id, "summary": title, "start": s, "end": e,
            "visibility": "private",
            "extendedProperties": {"private": {"anybao": "hold"}}}
    if desc:
        body["description"] = desc
    if loc:
        body["location"] = loc
    r = _request("post", _HOLD_PATH + _qs({"sendUpdates": "none"}), body)
    if not r["ok"]:
        if r.get("status") == 409 and key is not None:
            got = get_event(event_id)
            if not got.get("ok"):
                return got
            if (got.get("event") or {}).get("status") != "cancelled":
                return {"ok": True, "event": got["event"], "created": False}
            # Google keeps a deleted id as a cancelled event: bring it back
            restore = {k: v for k, v in body.items() if k != "id"}
            restore["status"] = "confirmed"
            r = _request("patch", f"{_HOLD_PATH}/{_quote(event_id)}"
                         + _qs({"sendUpdates": "none"}), restore)
            if not r["ok"]:
                return r
            return {"ok": True, "event": _trim_event(r["body"]), "created": True}
        return r
    return {"ok": True, "event": _trim_event(r["body"]), "created": True}


@span("googleCalendar.update_event", kind="mutator")  # noqa: F821 - guest global
def update_event(event_id, summary=None, start=None, end=None, description=None,
                 location=None, time_zone=None):
    """Change one of Bao's holds; omitted fields stay → {ok, event}.

    Only ids starting "baoagent" (create_event's), and only while the
    hold has no guests — once the user adds people it is their meeting.
    To move it, pass start AND end (same shapes as create_event)."""
    refused = _own_id(event_id)
    if refused:
        return refused
    body = {}
    try:
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("pass both start and end to move a hold")
            body["start"], body["end"] = _span(start, end, time_zone)
        for field, value in (("summary", summary), ("description", description),
                             ("location", location)):
            text = _text(value, field, required=field == "summary" and value is not None)
            if text is not None:
                body[field] = text
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    if not body:
        return {"ok": False, "error": "nothing to update — pass summary, start/end, "
                                      "description or location"}
    r = _request("patch", f"{_HOLD_PATH}/{_quote(event_id)}" + _qs({"sendUpdates": "none"}),
                 body)
    if not r["ok"]:
        return r
    return {"ok": True, "event": _trim_event(r["body"])}


@span("googleCalendar.delete_event", kind="mutator")  # noqa: F821 - guest global
def delete_event(event_id):
    """Remove one of Bao's holds → {ok, id, deleted}.

    Only "baoagent…" ids without guests; deleted=False when it was
    already gone."""
    refused = _own_id(event_id)
    if refused:
        return refused
    r = _request("delete", f"{_HOLD_PATH}/{_quote(event_id)}" + _qs({"sendUpdates": "none"}))
    if not r["ok"]:
        # the runtime's ownership read answers 404/410 for a gone hold
        if r.get("status") in (404, 410):
            return {"ok": True, "id": event_id, "deleted": False}
        return r
    return {"ok": True, "id": event_id, "deleted": True}


def main(args):
    args = args if isinstance(args, dict) else {}
    return upcoming(max_results=args.get("max_results"),
                    calendar_id=args.get("calendar_id"),
                    page_token=args.get("page_token"))
