"""Read-only Google Calendar connector — calendars, events, upcoming view.

The user's calendar list, events in a time window (recurrences
expanded, ordered by start), one event in full, and an upcoming view
from now. Events trim to the useful fields (summary, start/end,
attendees, location, conferenceLink, htmlLink). Incremental sync:
sync_token in, nextSyncToken out — a sync call sends ONLY token +
paging (Google rejects mixing syncToken with timeMin/orderBy/q).
Auth rides googleAuth@v1 (one consent covers the Google family) —
not-connected errors say to run googleAuth.connect(). Methods return
{ok, ...} or {ok: False, error}. Scope: calendar.readonly."""

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

_auth = use("googleAuth@v1")  # noqa: F821 - guest global
_CRED = _auth._CRED

_BASE = "https://www.googleapis.com/calendar/v3"
_DEFAULT_MAX = 50
_HARD_MAX = 250  # recurrence expansion can fan out fast
_MAX_RETRIES = 3
_TIMEOUT_S = 60

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
            if resp.status == 401:
                return {"ok": False, "status": 401,
                        "error": "Calendar rejected the token (HTTP 401) — run "
                                 "googleAuth.connect() in a user-facing turn to reauthorize."}
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
        try:
            return {"ok": True, "body": resp.json()}
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

    calendars are {id, summary, primary, accessRole, timeZone};
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


def main(args):
    args = args if isinstance(args, dict) else {}
    return upcoming(max_results=args.get("max_results"),
                    calendar_id=args.get("calendar_id"),
                    page_token=args.get("page_token"))
