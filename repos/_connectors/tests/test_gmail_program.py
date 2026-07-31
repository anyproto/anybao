"""programs/gmail@v1 through the REAL guest kernel. Inline fixtures
model live Gmail v1 replies. Pinned here: the trimmed message shape
(Gmail's own field names + the documented derived header/body keys,
C1), base64url MIME decoding (plain, nested multipart, html-only
fallback), repeated-key query assembly (labelIds), the managed-ref
credential plumbing (ADR-011 §4 full shape), the typed not-connected
contract (prefix match → googleAuth.connect() guidance), and 429
backoff through the sleep effect."""

import base64
import json

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}


def b64url(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def ok_json(body):
    return {"status": 200, "headers": {}, "body": json.dumps(body), "url": ""}


MESSAGE = {
    "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
    "historyId": "h9", "internalDate": "1785400000000",
    "snippet": "hey — quick question",
    "payload": {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "From", "value": "Boss <boss@example.com>"},
            {"name": "To", "value": "me@example.com"},
            {"name": "Subject", "value": "quick question"},
            {"name": "Date", "value": "Thu, 30 Jul 2026 09:00:00 +0200"},
        ],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64url("plain wins\nover html")}},
            {"mimeType": "text/html", "body": {"data": b64url("<p>plain wins</p>")}},
        ],
    },
}

TRIMMED = {
    "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
    "internalDate": "1785400000000",
    "from": "Boss <boss@example.com>", "to": "me@example.com", "cc": "",
    "subject": "quick question", "date": "Thu, 30 Jul 2026 09:00:00 +0200",
    "snippet": "hey — quick question", "body": "plain wins\nover html",
}


class FakeGmail:
    """Routes http.get by url substring — first match wins; a list
    value is a consumable script (429 retries); an Exception raises
    host-side → guest EffectError. Captures sleep effects."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.slept = []

    def __call__(self, name, payload):
        if name == "sleep":
            self.slept.append(payload["seconds"])
            return {"slept": payload["seconds"]}
        assert name == "http.get", f"unexpected effect {name}"
        assert payload["credential"] == CRED
        self.requests.append(payload)
        for pattern, resp in self.routes:
            if pattern in payload["url"]:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {payload['url']}")


def load(routes):
    fake = FakeGmail(routes)
    gm = connector_kernel(effect=fake).use("gmail@v1")
    return gm, fake


# ---- transport + auth contract ----------------------------------------------

def test_list_labels_and_credential_plumbing():
    gm, fake = load([("/labels", ok_json({"labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system",
         "messagesTotal": 991}]}))])
    out = gm.list_labels()
    # trimmed to id/name/type — the count fields stay behind
    assert out == {"ok": True, "labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"}]}
    req = fake.requests[0]
    assert req["url"].startswith("https://gmail.googleapis.com/gmail/v1/users/me")
    assert req["credential"] == CRED


def test_not_connected_maps_to_connect_help():
    not_connected = type("not_connected", (Exception,), {})(
        "google is not connected — run the provider's connect first")
    gm, _ = load([("/labels", not_connected)])
    out = gm.list_labels()
    assert out["ok"] is False
    assert out["error"].startswith("not_connected")
    assert "connect()" in out["error"]


def test_http_401_points_at_reconnect():
    gm, _ = load([("/labels",
                   {"status": 401, "headers": {}, "body": "{}", "url": ""})])
    out = gm.list_labels()
    assert out["ok"] is False and out["status"] == 401
    assert "googleAuth.connect()" in out["error"]


def test_429_backs_off_then_succeeds():
    gm, fake = load([("/labels", [
        {"status": 429, "headers": {"retry-after": "1"}, "body": "{}", "url": ""},
        ok_json({"labels": []}),
    ])])
    out = gm.list_labels()
    assert out == {"ok": True, "labels": []}
    assert fake.slept == [1]


# ---- listing + search -------------------------------------------------------

def test_list_messages_query_assembly():
    gm, fake = load([("/messages", ok_json({
        "messages": [{"id": "m1", "threadId": "t1"}],
        "resultSizeEstimate": 1}))])
    out = gm.list_messages(q="from:boss is:unread",
                           label_ids=["INBOX", "UNREAD"])
    assert out == {"ok": True, "messages": [{"id": "m1", "threadId": "t1"}],
                   "nextPageToken": None, "resultSizeEstimate": 1}
    url = fake.requests[0]["url"]
    assert "q=from%3Aboss%20is%3Aunread" in url
    assert "labelIds=INBOX&labelIds=UNREAD" in url  # repeated, not JSON-packed
    assert "maxResults=20" in url  # default page size


def test_search_requires_query():
    gm, fake = load([])
    assert gm.search("")["ok"] is False
    assert fake.requests == []


# ---- message decoding (the reason this connector exists) --------------------

def test_get_message_trims_and_decodes():
    gm, fake = load([("/messages/m1", ok_json(MESSAGE))])
    out = gm.get_message("m1")
    assert out == {"ok": True, "message": TRIMMED}
    assert "format=full" in fake.requests[0]["url"]


def test_get_message_html_only_falls_back_to_stripped_text():
    msg = json.loads(json.dumps(MESSAGE))
    msg["payload"]["parts"] = [
        {"mimeType": "text/html",
         "body": {"data": b64url("<p>only &amp; html</p>")}}]
    gm, _ = load([("/messages/m1", ok_json(msg))])
    out = gm.get_message("m1")
    assert out["message"]["body"] == "only & html"


def test_get_message_requires_id():
    gm, fake = load([])
    assert gm.get_message("")["ok"] is False
    assert fake.requests == []


def test_get_thread_orders_trimmed_messages():
    second = json.loads(json.dumps(MESSAGE))
    second["id"] = "m2"
    gm, _ = load([("/threads/t1", ok_json(
        {"id": "t1", "messages": [MESSAGE, second]}))])
    out = gm.get_thread("t1")
    assert out["ok"] is True and out["id"] == "t1"
    assert [m["id"] for m in out["messages"]] == ["m1", "m2"]
    assert out["messages"][0] == TRIMMED
