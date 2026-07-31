"""programs/googleCalendar@v1 through the REAL guest kernel
(connectorenv wires anybao's tests/kernelenv.py at this repo's
programs/). Fixtures are inline dicts modeled on Calendar REST v3
replies. Pinned here: the trimmed event shape (upstream names
verbatim, C1; conferenceLink is the one derived key), managed-ref
credential plumbing on every request, the typed not-connected
EffectError mapping to googleAuth._friendly's connect() guidance, the
syncToken-vs-window param exclusivity (a sync call must NOT send
timeMin/timeMax/orderBy/q — Google rejects the mix), and upcoming()'s
timeMin coming from the recorded time.now effect."""

import datetime
import json

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}
EPOCH = 1785456000.0  # 2026-07-31T00:00:00Z
STAMP = datetime.datetime.fromtimestamp(
    EPOCH, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

# The host's typed failure contract (ADR-011): EffectError arrives as
# "type: message" — kernelenv builds it from the raised exception's
# CLASS NAME + str, so an exception class literally named
# not_connected yields the prefix the connector matches on.
NotConnected = type("not_connected", (Exception,), {})


def ok(payload, status=200, headers=None):
    return {"status": status, "headers": headers or {},
            "body": json.dumps(payload), "url": ""}


RAW_EVENT = {
    "kind": "calendar#event", "etag": '"3391"',
    "id": "ev_abc123", "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "created": "2026-07-01T08:00:00.000Z",
    "updated": "2026-07-30T09:15:00.000Z",
    "summary": "Sync weekly", "description": "agenda in doc",
    "location": "Berlin HQ",
    "creator": {"email": "boss@anyorg.io"},
    "organizer": {"email": "boss@anyorg.io", "displayName": "Boss"},
    "start": {"dateTime": "2026-07-31T10:00:00+02:00",
              "timeZone": "Europe/Berlin"},
    "end": {"dateTime": "2026-07-31T11:00:00+02:00",
            "timeZone": "Europe/Berlin"},
    "recurringEventId": "ev_abc123_r",
    "iCalUID": "ev_abc123@google.com", "sequence": 2,
    "attendees": [
        {"email": "boss@anyorg.io", "displayName": "Boss",
         "organizer": True, "responseStatus": "accepted"},
        {"email": "me@anyorg.io", "self": True,
         "responseStatus": "needsAction", "comment": "maybe"}],
    "hangoutLink": "https://meet.google.com/xyz-abcd-efg",
    "conferenceData": {"entryPoints": [
        {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg"}]},
    "reminders": {"useDefault": True}, "eventType": "default",
}

TRIMMED_EVENT = {
    "id": "ev_abc123", "summary": "Sync weekly", "status": "confirmed",
    "start": {"dateTime": "2026-07-31T10:00:00+02:00",
              "timeZone": "Europe/Berlin"},
    "end": {"dateTime": "2026-07-31T11:00:00+02:00",
            "timeZone": "Europe/Berlin"},
    "attendees": [
        {"email": "boss@anyorg.io", "responseStatus": "accepted",
         "displayName": "Boss", "organizer": True},
        {"email": "me@anyorg.io", "responseStatus": "needsAction",
         "self": True}],
    "location": "Berlin HQ", "description": "agenda in doc",
    "organizer": {"email": "boss@anyorg.io", "displayName": "Boss"},
    "conferenceLink": "https://meet.google.com/xyz-abcd-efg",
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "updated": "2026-07-30T09:15:00.000Z",
    "recurringEventId": "ev_abc123_r",
}


class FakeGoogle:
    """Serves http.get + sleep + time.now from ordered (url-substring
    → response) routes — first match wins. Asserts the managed
    credential ref on every http request; an Exception value is
    raised host-side → guest EffectError."""

    def __init__(self, routes, epoch=EPOCH):
        self.routes = list(routes)
        self.requests = []
        self.slept = []
        self.epoch = epoch

    def __call__(self, name, payload):
        if name == "time.now":
            return {"epoch": self.epoch}
        if name == "sleep":
            self.slept.append(payload["seconds"])
            return {"slept": payload["seconds"]}
        assert name == "http.get", f"unexpected effect {name}"
        # tokens never cross the boundary — only the managed ref does
        assert payload["credential"] == CRED
        self.requests.append(payload)
        url = payload["url"]
        for pattern, resp in self.routes:
            if pattern in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {url}")


def load(routes):
    fake = FakeGoogle(routes)
    gc = connector_kernel(effect=fake).use("googleCalendar@v1")
    return gc, fake


# ---- auth + request plumbing ------------------------------------------------

def test_list_calendars_trim_and_credential_plumbing():
    gc, fake = load([("/users/me/calendarList", ok({
        "kind": "calendar#calendarList",
        "items": [
            {"kind": "calendar#calendarListEntry", "id": "me@anyorg.io",
             "summary": "me@anyorg.io", "primary": True,
             "accessRole": "owner", "timeZone": "Europe/Berlin",
             "colorId": "14", "selected": True,
             "defaultReminders": [{"method": "popup", "minutes": 10}]},
            {"id": "team@group.calendar.google.com", "summary": "Team",
             "accessRole": "reader", "timeZone": "UTC"}],
        "nextPageToken": "cal-page-2"}))])
    out = gc.list_calendars()
    assert out == {"ok": True, "calendars": [
        {"id": "me@anyorg.io", "summary": "me@anyorg.io", "primary": True,
         "accessRole": "owner", "timeZone": "Europe/Berlin"},
        {"id": "team@group.calendar.google.com", "summary": "Team",
         "primary": False, "accessRole": "reader", "timeZone": "UTC"}],
        "nextPageToken": "cal-page-2"}
    url = fake.requests[0]["url"]
    assert url.startswith(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList")
    assert "maxResults=50" in url  # default page clamp
    assert fake.requests[0]["credential"] == CRED


def test_not_connected_surfaces_friendly_connect_message():
    gc, _ = load([("/users/me/calendarList", NotConnected(
        "google is not connected — run the provider's connect first"))])
    out = gc.list_calendars()
    assert out["ok"] is False
    assert out["error"].startswith("not_connected: google is not connected")
    assert "connect()" in out["error"]


# ---- events -----------------------------------------------------------------

def test_list_events_trims_c1():
    gc, fake = load([("/calendars/primary/events", ok({
        "kind": "calendar#events", "summary": "me@anyorg.io",
        "timeZone": "Europe/Berlin", "accessRole": "owner",
        "items": [RAW_EVENT], "nextPageToken": "ev-page-2",
        "nextSyncToken": "sync-tok-9"}))])
    out = gc.list_events(time_min="2026-07-01T00:00:00Z", q="sync")
    assert out["ok"] is True and out["calendarId"] == "primary"
    assert out["events"] == [TRIMMED_EVENT]  # exact keys, upstream names
    assert out["nextPageToken"] == "ev-page-2"
    assert out["nextSyncToken"] == "sync-tok-9"
    assert out["timeZone"] == "Europe/Berlin"
    url = fake.requests[0]["url"]
    assert "singleEvents=true" in url and "orderBy=startTime" in url
    assert "timeMin=2026-07-01T00%3A00%3A00Z" in url and "q=sync" in url


def test_sync_token_excludes_window_params():
    gc, fake = load([("/calendars/primary/events", ok({
        "items": [], "nextSyncToken": "sync-tok-10"}))])
    out = gc.list_events(sync_token="sync-tok-9",
                         time_min="2026-07-01T00:00:00Z", q="standup")
    assert out["ok"] is True and out["nextSyncToken"] == "sync-tok-10"
    url = fake.requests[0]["url"]
    assert "syncToken=sync-tok-9" in url
    # Google rejects mixing syncToken with the window params — none may cross
    assert "timeMin" not in url and "timeMax" not in url
    assert "orderBy" not in url and "q=" not in url
    assert "singleEvents" not in url


def test_get_event_conference_link_falls_back_to_entry_point():
    raw = {k: v for k, v in RAW_EVENT.items() if k != "hangoutLink"}
    gc, fake = load([("/calendars/team%40group.calendar.google.com/events/ev_abc123",
                      ok(raw))])
    out = gc.get_event("ev_abc123", calendar_id="team@group.calendar.google.com")
    assert out["ok"] is True
    # derived key: no hangoutLink → first video entryPoint uri
    assert out["event"]["conferenceLink"] == "https://meet.google.com/xyz-abcd-efg"
    assert out["event"]["id"] == "ev_abc123"
    assert fake.requests[0]["credential"] == CRED


def test_get_event_requires_event_id():
    gc, fake = load([])
    out = gc.get_event("")
    assert out["ok"] is False and "event_id is required" in out["error"]
    assert fake.requests == []  # nothing crossed the boundary


def test_upcoming_time_min_is_now_rfc3339():
    gc, fake = load([("/calendars/primary/events", ok({"items": []}))])
    out = gc.upcoming(max_results=5)
    assert out["ok"] is True and out["calendarId"] == "primary"
    url = fake.requests[0]["url"]
    assert "timeMin=" + STAMP.replace(":", "%3A") in url
    assert "maxResults=5" in url
