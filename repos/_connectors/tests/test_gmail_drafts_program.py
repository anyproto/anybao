"""programs/gmail@v1 drafts (ADR-034 §4, §7) through the REAL guest
kernel. Pinned here: the message the connector builds is what the
runtime's draft check admits (ASCII header block, one text/plain
Content-Type, the X-Anybao-Draft marker) and what Gmail threads (the
original's threadId, In-Reply-To, References, a "Re:" subject); header
injection and non-addresses never reach the wire; a runtime refusal
comes back as {ok: False, refused: True} with a do-not-retry hint;
update/delete keep to Bao's own drafts."""

import base64
import email
import email.policy
import json

from connectorenv import connector_kernel

CRED = {"ref": "connector.oauth.google", "header": "Authorization",
        "prefix": "Bearer "}
BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def typed(type_name, message):
    """An exception whose class name is the runtime failure type."""
    return type(type_name, (Exception,), {})(message)


def ok_json(body, status=200):
    return {"status": status, "headers": {}, "body": json.dumps(body), "url": ""}


NO_CONTENT = {"status": 204, "headers": {}, "body": "", "url": ""}


class FakeGmail:
    """Routes http.<verb> by (verb, url substring) — first match wins;
    an Exception raises host-side → guest EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []

    def __call__(self, name, payload):
        assert name.startswith("http."), f"unexpected effect {name}"
        assert payload["credential"] == CRED
        verb = name.split(".", 1)[1]
        self.requests.append((verb, payload))
        for (rverb, pattern), resp in self.routes:
            if rverb == verb and pattern in payload["url"]:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {verb} {payload['url']}")

    def writes(self):
        return [(v, p) for v, p in self.requests if v != "get"]


def load(routes):
    fake = FakeGmail(routes)
    gm = connector_kernel(effect=fake).use("gmail@v1")
    return gm, fake


def decode(raw):
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return data, email.message_from_bytes(data, policy=email.policy.default)


def body_of(msg):
    return msg.get_content().replace("\r\n", "\n")


def assert_runtime_admits(data):
    """The invariants runtime/src/oauth_writes.rs `gmail_draft_text`
    checks, restated: an ASCII header block of well-formed lines, at
    most one Content-Type and it is text/plain, the marker present."""
    head = data.split(b"\r\n\r\n", 1)[0]
    assert head.isascii()
    assert b"\n" not in head.replace(b"\r\n", b"")
    lines = head.decode().split("\r\n")
    names = []
    for line in lines:
        if line[:1] in (" ", "\t"):
            continue
        name, _, _ = line.partition(":")
        assert name and " " not in name, line
        names.append(name.lower())
    assert names.count("content-type") == 1
    ctype = next(line for line in lines if line.lower().startswith("content-type:"))
    assert ctype.split(":", 1)[1].split(";")[0].strip().lower() == "text/plain"
    assert "x-anybao-draft" in names


DRAFT_OUT = {"id": "r-1", "message": {"id": "m9", "threadId": "t9",
                                      "labelIds": ["DRAFT"]}}
TRIMMED_OUT = {"id": "r-1", "message": {"id": "m9", "threadId": "t9",
                                        "labelIds": ["DRAFT"]}}

ORIGINAL = {
    "id": "m1", "threadId": "t1",
    "payload": {"headers": [
        {"name": "Message-Id", "value": "<orig@mail.example.com>"},
        {"name": "References", "value": "<root@mail.example.com>"},
        {"name": "Subject", "value": "quick question"},
        {"name": "From", "value": "Boss <boss@example.com>"},
        {"name": "To", "value": "me@example.com, Ann <ann@x.io>"},
        {"name": "Cc", "value": "bob@x.io, Me <ME@example.com>"},
    ]},
}


# ---- create -----------------------------------------------------------------

def test_create_draft_builds_a_marked_plain_text_message():
    gm, fake = load([(("post", "/drafts"), ok_json(DRAFT_OUT))])
    out = gm.create_draft(to=["Ann <ann@x.io>", "bob@x.io"], subject="Grüße — plan",
                          body="Hallo Ann,\n\nlooks good — see you at 3.\n",
                          cc="carol@x.io", bcc=["dan@x.io"])
    assert out == {"ok": True, "draft": TRIMMED_OUT}
    [(verb, payload)] = fake.writes()
    assert verb == "post"
    assert payload["url"] == BASE + "/drafts"
    assert set(payload["json"]) == {"message"}
    assert set(payload["json"]["message"]) == {"raw"}  # no thread: not a reply
    data, msg = decode(payload["json"]["message"]["raw"])
    assert_runtime_admits(data)
    assert msg["To"] == "Ann <ann@x.io>, bob@x.io"
    assert msg["Cc"] == "carol@x.io"
    assert msg["Bcc"] == "dan@x.io"
    assert msg["Subject"] == "Grüße — plan"  # RFC 2047 on the wire, decoded here
    assert msg["X-Anybao-Draft"] == "1"
    assert msg.get_content_type() == "text/plain"
    # CRLF on the wire (RFC 5322); the text round-trips
    assert body_of(msg) == "Hallo Ann,\n\nlooks good — see you at 3.\n"


def test_create_draft_threads_a_reply():
    gm, fake = load([(("get", "/messages/m1"), ok_json(ORIGINAL)),
                     (("post", "/drafts"), ok_json(DRAFT_OUT))])
    out = gm.create_draft(reply_to="m1", body="Works for me.")
    assert out["ok"] is True
    get_url = fake.requests[0][1]["url"]
    assert "format=metadata" in get_url
    assert "metadataHeaders=Message-ID&metadataHeaders=References" in get_url
    payload = fake.writes()[0][1]
    assert payload["json"]["message"]["threadId"] == "t1"
    data, msg = decode(payload["json"]["message"]["raw"])
    assert_runtime_admits(data)
    assert msg["To"] == "Boss <boss@example.com>"
    assert msg["Cc"] is None
    assert msg["Subject"] == "Re: quick question"
    assert msg["In-Reply-To"] == "<orig@mail.example.com>"
    assert msg["References"] == "<root@mail.example.com> <orig@mail.example.com>"


def test_reply_all_adds_the_others_minus_the_user():
    gm, fake = load([(("get", "/messages/m1"), ok_json(ORIGINAL)),
                     (("get", "/profile"), ok_json({"emailAddress": "me@example.com"})),
                     (("post", "/drafts"), ok_json(DRAFT_OUT))])
    out = gm.create_draft(reply_to="m1", reply_all=True, body="+1")
    assert out["ok"] is True
    _, msg = decode(fake.writes()[0][1]["json"]["message"]["raw"])
    assert msg["To"] == "Boss <boss@example.com>, Ann <ann@x.io>"
    assert msg["Cc"] == "bob@x.io"


def test_a_reply_keeps_an_existing_re_and_reply_to_header():
    original = json.loads(json.dumps(ORIGINAL))
    original["payload"]["headers"] += [
        {"name": "Reply-To", "value": "list@x.io"}]
    original["payload"]["headers"][2]["value"] = "RE: quick\r\n question"
    gm, fake = load([(("get", "/messages/m1"), ok_json(original)),
                     (("post", "/drafts"), ok_json(DRAFT_OUT))])
    assert gm.create_draft(reply_to="m1", body="x")["ok"] is True
    _, msg = decode(fake.writes()[0][1]["json"]["message"]["raw"])
    assert msg["Subject"] == "RE: quick question"  # unfolded, not "Re: RE:"
    assert msg["To"] == "list@x.io"


def test_bad_input_never_reaches_the_wire():
    gm, fake = load([])
    cases = [
        dict(to="ann@x.io", subject="hi\r\nBcc: evil@x.io", body="x"),
        dict(to="ann@x.io\nBcc: evil@x.io", body="x"),
        dict(to="not-an-address", body="x"),
        dict(to=["ann@x.io", 5], body="x"),
        dict(body="nobody to send to"),
        dict(to="ann@x.io", reply_all=True, body="x"),
        dict(to="ann@x.io", body=b"bytes"),
        dict(to="ann@x.io", body="x" * 100_001),
        dict(to="ann@x.io", reply_to=["m1"], body="x"),
    ]
    for kw in cases:
        out = gm.create_draft(**kw)
        assert out["ok"] is False, kw
        assert out["error"], kw
    assert fake.requests == []


def test_a_runtime_refusal_is_final_and_says_so():
    refused = typed("credential_write_refused",
                    'credential ref "connector.oauth.google" refuses this request: '
                    "a draft must be text/plain (ADR-034)")
    gm, _ = load([(("post", "/drafts"), refused)])
    out = gm.create_draft(to="ann@x.io", body="x")
    assert out["ok"] is False and out["refused"] is True
    assert out["error"].startswith("credential_write_refused")
    assert "Do not retry" in out["error"]


def test_a_read_only_grant_points_at_connect_writes():
    gm, _ = load([(("post", "/drafts"), ok_json({"error": {"message": "scopes"}}, 403))])
    out = gm.create_draft(to="ann@x.io", body="x")
    assert out["ok"] is False and out["status"] == 403
    assert "googleAuth.connect(writes=True)" in out["error"]


def test_not_connected_maps_to_connect_help_on_writes_too():
    gm, _ = load([(("post", "/drafts"),
                   typed("not_connected", "google is not connected"))])
    out = gm.create_draft(to="ann@x.io", body="x")
    assert out["ok"] is False and "connect()" in out["error"]


# ---- read -------------------------------------------------------------------

def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def stored_draft(marked=True):
    headers = [
        {"name": "To", "value": "Ann <ann@x.io>"},
        {"name": "Bcc", "value": "dan@x.io"},
        {"name": "Subject", "value": "Re: plan"},
        {"name": "In-Reply-To", "value": "<orig@mail.example.com>"},
        {"name": "References", "value": "<orig@mail.example.com>"},
    ]
    if marked:
        headers.append({"name": "X-Anybao-Draft", "value": "1"})
    return {"id": "r-1", "message": {
        "id": "m9", "threadId": "t1", "labelIds": ["DRAFT"],
        "internalDate": "1785400000000", "snippet": "old text",
        "payload": {"mimeType": "text/plain", "headers": headers,
                    "body": {"data": _b64("old text\n")}}}}


def test_list_and_get_drafts():
    gm, fake = load([
        (("get", "/drafts/r-1"), ok_json(stored_draft())),
        (("get", "/drafts"), ok_json({"drafts": [{"id": "r-1", "message":
                                                  {"id": "m9", "threadId": "t1"}}],
                                      "resultSizeEstimate": 1})),
    ])
    out = gm.list_drafts(q="to:ann")
    assert out == {"ok": True, "drafts": [{"id": "r-1", "message":
                                           {"id": "m9", "threadId": "t1"}}],
                   "nextPageToken": None, "resultSizeEstimate": 1}
    assert "q=to%3Aann" in fake.requests[0][1]["url"]
    got = gm.get_draft("r-1")
    assert got["ok"] is True and got["draft"]["bao"] is True
    m = got["draft"]["message"]
    assert (m["to"], m["bcc"], m["subject"], m["body"]) == (
        "Ann <ann@x.io>", "dan@x.io", "Re: plan", "old text\n")


def test_get_profile_keeps_gmails_names():
    gm, _ = load([(("get", "/profile"), ok_json({
        "emailAddress": "me@example.com", "messagesTotal": 10,
        "threadsTotal": 7, "historyId": "99"}))])
    assert gm.get_profile() == {"ok": True, "emailAddress": "me@example.com",
                                "messagesTotal": 10, "threadsTotal": 7,
                                "historyId": "99"}


# ---- update / delete --------------------------------------------------------

def test_update_draft_keeps_what_it_is_not_given():
    gm, fake = load([(("get", "/drafts/r-1"), ok_json(stored_draft())),
                     (("put", "/drafts/r-1"), ok_json(DRAFT_OUT))])
    out = gm.update_draft("r-1", body="new text")
    assert out == {"ok": True, "draft": TRIMMED_OUT}
    [(verb, payload)] = fake.writes()
    assert verb == "put" and payload["url"] == BASE + "/drafts/r-1"
    assert payload["json"]["id"] == "r-1"
    assert payload["json"]["message"]["threadId"] == "t1"
    data, msg = decode(payload["json"]["message"]["raw"])
    assert_runtime_admits(data)
    assert msg["To"] == "Ann <ann@x.io>" and msg["Bcc"] == "dan@x.io"
    assert msg["Subject"] == "Re: plan"
    assert msg["In-Reply-To"] == "<orig@mail.example.com>"
    assert body_of(msg) == "new text\n"


def test_update_draft_leaves_the_users_own_alone():
    gm, fake = load([(("get", "/drafts/r-1"), ok_json(stored_draft(marked=False)))])
    out = gm.update_draft("r-1", body="mine now")
    assert out["ok"] is False and out["refused"] is True
    assert "the user's" in out["error"]
    assert fake.writes() == []


def test_delete_draft():
    gm, fake = load([(("delete", "/drafts/r-1"), NO_CONTENT)])
    assert gm.delete_draft("r-1") == {"ok": True, "id": "r-1", "deleted": True}
    assert fake.writes()[0][1]["url"] == BASE + "/drafts/r-1"
    assert gm.delete_draft("")["ok"] is False


def test_delete_draft_maps_refusals():
    gone = typed("credential_write_refused",
                 "could not verify the item is Bao's: the ownership read answered "
                 "HTTP 404 (ADR-034)")
    gm, _ = load([(("delete", "/drafts/r-1"), gone)])
    assert gm.delete_draft("r-1") == {"ok": True, "id": "r-1", "deleted": False}
    theirs = typed("credential_write_refused",
                   "the draft has no X-Anybao-Draft header (ADR-034)")
    gm, _ = load([(("delete", "/drafts/r-2"), theirs)])
    out = gm.delete_draft("r-2")
    assert out["ok"] is False and out["refused"] is True
