"""programs/googleCalendar@v1 holds (ADR-034 §5, §7) through the REAL
guest kernel. Pinned here: every write targets the primary calendar
with sendUpdates=none and a json= body the runtime's hold checks admit
(a base32hex `baoagent…` id, visibility private, no attendees); keyed
creates are idempotent (409 → the existing hold, created=False); bad
times never reach the wire; update/delete refuse foreign ids before
any request; a runtime refusal comes back final."""

import json
import re

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}
CRED_WRITE = {"ref": "connector.oauth.google_write", "header": "Authorization",
              "prefix": "Bearer "}
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
HOLD_ID = re.compile(r"baoagent[0-9a-f]{32}")


def typed(type_name, message):
    return type(type_name, (Exception,), {})(message)


def ok_json(body, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(body), "url": ""}


class FakeCalendar:
    """Routes http.<verb> by (verb, url substring), first match wins;
    a callable response gets the payload; serves time.now; an
    Exception raises host-side → guest EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []

    def __call__(self, name, payload):
        if name == "time.now":
            return {"epoch": 1785400000.0}
        assert name.startswith("http."), f"unexpected effect {name}"
        verb = name.split(".", 1)[1]
        assert payload["credential"] == (CRED if verb == "get" else CRED_WRITE), verb
        self.requests.append((verb, payload))
        for (rverb, pattern), resp in self.routes:
            if rverb == verb and pattern in payload["url"]:
                if isinstance(resp, Exception):
                    raise resp
                return resp(payload) if callable(resp) else resp
        raise AssertionError(f"no route for {verb} {payload['url']}")

    def writes(self):
        return [(v, p) for v, p in self.requests if v != "get"]


def load(routes):
    fake = FakeCalendar(routes)
    gc = connector_kernel(effect=fake).use("googleCalendar@v1")
    return gc, fake


def echo(body):
    """Google's reply to an insert: the event as stored."""
    out = dict(body)
    out.update({"status": "confirmed", "htmlLink": "https://calendar.google.com/e",
                "organizer": {"email": "me@example.com", "self": True},
                "updated": "2026-11-09T18:00:00Z"})
    return out


def assert_runtime_admits(verb, payload):
    """runtime/src/oauth_writes.rs's hold rules, restated."""
    url = payload["url"]
    assert url.startswith(EVENTS)
    assert "sendUpdates=none" in url and "sendNotifications" not in url
    if verb == "post":
        body = payload["json"]
        assert HOLD_ID.fullmatch(body["id"])
        assert body["visibility"] == "private"
        assert "attendees" not in body
    else:
        assert url.split("?")[0].rsplit("/", 1)[1].startswith("baoagent")
        assert "attendees" not in (payload.get("json") or {})


# ---- create -----------------------------------------------------------------

def test_create_event_writes_a_private_solo_hold():
    gc, fake = load([(("post", "/calendars/primary/events"),
                      lambda payload: ok_json(echo(payload["json"])))])
    out = gc.create_event("Focus: Q4 plan", "2026-11-10T15:00:00+01:00",
                          "2026-11-10T16:30:00+01:00", description="draft the plan",
                          location="home")
    assert out["ok"] is True and out["created"] is True
    [(verb, payload)] = fake.writes()
    assert_runtime_admits(verb, payload)
    body = payload["json"]
    assert body["summary"] == "Focus: Q4 plan"
    assert body["start"] == {"dateTime": "2026-11-10T15:00:00+01:00"}
    assert body["end"] == {"dateTime": "2026-11-10T16:30:00+01:00"}
    assert body["description"] == "draft the plan" and body["location"] == "home"
    assert body["extendedProperties"] == {"private": {"anybao": "hold"}}
    ev = out["event"]
    assert ev["id"] == body["id"] and ev["summary"] == "Focus: Q4 plan"
    assert ev["attendees"] == []


def test_all_day_and_time_zone_holds():
    gc, fake = load([(("post", "/calendars/primary/events"), ok_json({"id": "x"}))])
    assert gc.create_event("Offsite", "2026-11-10", "2026-11-12")["ok"] is True
    assert fake.writes()[0][1]["json"]["start"] == {"date": "2026-11-10"}
    assert gc.create_event("Call", "2026-11-10T15:00:00", "2026-11-10T15:30:00",
                           time_zone="Europe/Berlin")["ok"] is True
    body = fake.writes()[1][1]["json"]
    assert body["start"] == {"dateTime": "2026-11-10T15:00:00",
                             "timeZone": "Europe/Berlin"}


def test_bad_times_never_reach_the_wire():
    gc, fake = load([])
    cases = [
        ("", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z", {}),
        ("x", "tomorrow", "2026-11-10T11:00:00Z", {}),
        ("x", "2026-11-10T11:00:00Z", "2026-11-10T10:00:00Z", {}),
        ("x", "2026-11-10", "2026-11-10", {}),
        ("x", "2026-11-10", "2026-11-10T11:00:00Z", {}),
        ("x", "2026-11-10T10:00:00", "2026-11-10T11:00:00", {}),
        ("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00", {"time_zone": "UTC"}),
        ("x", "2026-02-30", "2026-03-02", {}),
        ("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z", {"key": ""}),
        ("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z", {"description": 5}),
    ]
    for summary, start, end, kw in cases:
        out = gc.create_event(summary, start, end, **kw)
        assert out["ok"] is False and out["error"], (summary, start, end, kw)
    one_day = gc.create_event("x", "2026-11-10", "2026-11-10")
    assert "exclusive" in one_day["error"]
    assert fake.requests == []


def test_keyed_create_is_idempotent():
    conflict = ok_json({"error": {"message": "The requested identifier already exists."}},
                       409)
    gc, fake = load([
        (("post", "/calendars/primary/events"), conflict),
        (("get", "/calendars/primary/events/baoagent"),
         ok_json({"id": "existing", "summary": "Follow up"})),
    ])
    out = gc.create_event("Follow up", "2026-11-10T10:00:00Z",
                          "2026-11-10T10:15:00Z", key="followup:t1")
    assert out["ok"] is True and out["created"] is False
    assert out["event"]["summary"] == "Follow up"
    first_id = fake.writes()[0][1]["json"]["id"]
    # the id is a pure function of the key: a re-run asks for the same one
    gc2, fake2 = load([(("post", "/calendars/primary/events"), conflict),
                       (("get", "/calendars/primary/events/baoagent"),
                        ok_json({"id": "existing"}))])
    gc2.create_event("Other title", "2026-12-01", "2026-12-02", key="followup:t1")
    assert fake2.writes()[0][1]["json"]["id"] == first_id
    assert HOLD_ID.fullmatch(first_id)


def test_an_unkeyed_conflict_is_an_error():
    conflict = ok_json({"error": {"message": "exists"}}, 409)
    gc, _ = load([(("post", "/calendars/primary/events"), conflict)])
    out = gc.create_event("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z")
    assert out["ok"] is False and out["status"] == 409


def test_create_maps_refusals_and_scopes():
    refused = typed("credential_write_refused",
                    "a hold has no attendees — Bao cannot invite anyone (ADR-034)")
    gc, _ = load([(("post", "/calendars/primary/events"), refused)])
    out = gc.create_event("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z")
    assert out["ok"] is False and out["refused"] is True
    assert "Do not retry" in out["error"]
    gc, _ = load([(("post", "/calendars/primary/events"),
                   ok_json({"error": {"message": "insufficient"}}, 403))])
    out = gc.create_event("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z")
    assert "googleAuth.connect(writes=True)" in out["error"]


# ---- update / delete --------------------------------------------------------

HOLD = "baoagent0123456789abcdef0123456789ab"


def test_update_event_patches_only_what_is_given():
    gc, fake = load([(("patch", f"/events/{HOLD}"),
                      ok_json({"id": HOLD, "summary": "Moved"}))])
    out = gc.update_event(HOLD, summary="Moved", start="2026-11-11T09:00:00Z",
                          end="2026-11-11T10:00:00Z")
    assert out["ok"] is True and out["event"]["summary"] == "Moved"
    [(verb, payload)] = fake.writes()
    assert verb == "patch"
    assert_runtime_admits(verb, payload)
    assert payload["json"] == {"summary": "Moved",
                               "start": {"dateTime": "2026-11-11T09:00:00Z"},
                               "end": {"dateTime": "2026-11-11T10:00:00Z"}}


def test_update_event_refuses_before_any_request():
    gc, fake = load([])
    for event_id, kw in [
        ("7kq9l2abc", {"summary": "x"}),     # the user's own event
        (None, {"summary": "x"}),
        (HOLD, {}),                          # nothing to change
        (HOLD, {"start": "2026-11-11T09:00:00Z"}),  # start without end
        (HOLD, {"summary": ""}),             # cannot blank the title
    ]:
        out = gc.update_event(event_id, **kw)
        assert out["ok"] is False, (event_id, kw)
    assert gc.update_event("7kq9l2abc", summary="x")["refused"] is True
    assert fake.requests == []


def test_a_hold_turned_meeting_is_the_users():
    refused = typed("credential_write_refused",
                    "the event has guests — a hold the user turned into a meeting is "
                    "theirs (ADR-034)")
    gc, _ = load([(("patch", f"/events/{HOLD}"), refused)])
    out = gc.update_event(HOLD, summary="x")
    assert out["ok"] is False and out["refused"] is True and "guests" in out["error"]


def test_delete_event():
    gc, fake = load([(("delete", f"/events/{HOLD}"),
                      {"status": 204, "headers": {}, "body": "", "url": ""})])
    assert gc.delete_event(HOLD) == {"ok": True, "id": HOLD, "deleted": True}
    assert_runtime_admits(*fake.writes()[0])
    assert gc.delete_event("someone-elses")["refused"] is True
    gone = typed("credential_write_refused",
                 "could not verify the item is Bao's: the ownership read answered "
                 "HTTP 410 (ADR-034)")
    gc, _ = load([(("delete", f"/events/{HOLD}"), gone)])
    assert gc.delete_event(HOLD) == {"ok": True, "id": HOLD, "deleted": False}


def test_holds_need_the_write_consent():
    gc, _ = load([(("post", "/calendars/primary/events"),
                   typed("not_connected", "google_write is not connected"))])
    out = gc.create_event("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z")
    assert out["ok"] is False and "connect(writes=True)" in out["error"]
    gc, _ = load([(("post", "/calendars/primary/events"),
                   typed("not_configured", 'no oauth provider "google_write"'))])
    out = gc.create_event("x", "2026-11-10T10:00:00Z", "2026-11-10T11:00:00Z")
    assert out["ok"] is False and "predates the write guard" in out["error"]
