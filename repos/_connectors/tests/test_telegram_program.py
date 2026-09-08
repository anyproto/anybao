"""programs/telegram@v1 through the REAL guest kernel (connectorenv
wires anybao's tests/kernelenv.py at this repo's programs/). Fixtures
are inline Bot API envelopes modeled on live api.telegram.org replies.
Pinned here: the url-injected credential (ADR-008 §1 — `in: "url"`, the
`{credential}` marker, no header and no token anywhere in guest code),
the {ok, ...} envelopes with Telegram's own field names (message_id,
chat.id, file_id), the derived next_offset a poll loop runs on, the
error contract (401 / 409 / 400 / missing secret → an actionable
{ok: False, error}, never a traceback), and 429 backoff on
parameters.retry_after."""

import json

from connectorenv import connector_kernel

API = "https://api.telegram.org/bot{credential}"


def ok(payload, status=200):
    return {"status": status, "headers": {},
            "body": json.dumps({"ok": True, "result": payload}), "url": ""}


def err(code, description, status=None, parameters=None):
    body = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return {"status": status or code, "headers": {}, "body": json.dumps(body), "url": ""}


USER = {"id": 8100123, "is_bot": True, "first_name": "bao", "username": "bao_bot",
        "can_join_groups": True, "can_read_all_group_messages": False,
        "supports_inline_queries": False}

MESSAGE = {
    "message_id": 412, "date": 1757000000,
    "from": {"id": 55501, "is_bot": False, "first_name": "Anatolii",
             "username": "zarkone", "language_code": "en"},
    "chat": {"id": 55501, "type": "private", "first_name": "Anatolii",
             "username": "zarkone"},
    "text": "what did I say about the cows?",
}


class FakeTelegram:
    """Serves http.* + sleep from ordered (url-substring → response)
    routes — first match wins. A list value is a consumable sequence
    (retry scripts); an Exception is raised host-side → EffectError."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.requests = []
        self.slept = []

    def __call__(self, name, payload):
        if name == "sleep":
            self.slept.append(payload["seconds"])
            return {"slept": payload["seconds"]}
        assert name.startswith("http."), f"unexpected effect {name}"
        self.requests.append({"verb": name[len("http."):], **payload})
        url = payload["url"]
        for pattern, resp in self.routes:
            if pattern in url:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"no route for {url}")


def load(routes):
    fake = FakeTelegram(routes)
    tg = connector_kernel(effect=fake).use("telegram@v1")
    return tg, fake


# ---- auth plumbing (ADR-008 §1) --------------------------------------------

def test_me_and_url_injected_credential():
    tg, fake = load([("/getMe", ok(USER))])
    assert tg.me() == {"ok": True, "user": USER}
    req = fake.requests[0]
    # the token is a path segment Telegram has no header for: the guest
    # writes the marker, the host substitutes after recording
    assert req["url"] == API + "/getMe"
    assert "{credential}" in req["url"]
    cred = req["credential"]
    assert cred["ref"] == "connector.key.telegram"
    assert cred["in"] == "url"
    assert "header" not in cred and "prefix" not in cred
    # ADR-021 §1: the descriptor rides along, host-bound
    assert cred["about"]["hosts"] == ["api.telegram.org"]
    assert "BotFather" in cred["about"]["help"]


def test_no_token_anywhere_in_the_source():
    """The one property the whole design exists for: nothing in the
    program can hold or read the value."""
    src = (__import__("pathlib").Path(__file__).parents[1]
           / "programs" / "telegram@v1" / "program.py").read_text()
    assert "config.get" not in src and "agent_secrets" not in src
    # both outbound doors name the ref: the _call chokepoint and the
    # file-download hop, which is the one http.* outside it
    assert src.count("_CRED") == 3  # the definition + both call sites
    assert src.count("http.") == 2


def test_missing_secret_explains_how_to_connect():
    tg, _ = load([("/getMe", RuntimeError(
        'no secret for credential ref "connector.key.telegram"'))])
    out = tg.me()
    assert out["ok"] is False
    assert "Telegram not connected" in out["error"]
    assert "BotFather" in out["error"] and "connector.key.telegram" in out["error"]


# ---- reading: getUpdates ----------------------------------------------------

def test_get_updates_derives_next_offset_and_trims_messages():
    photo_msg = dict(MESSAGE, message_id=413, text=None, caption="the herd",
                     photo=[{"file_id": "small", "file_unique_id": "u1",
                             "width": 90, "height": 60, "file_size": 1200},
                            {"file_id": "big", "file_unique_id": "u2",
                             "width": 1280, "height": 853, "file_size": 98000}])
    photo_msg.pop("text")
    tg, fake = load([("/getUpdates", ok([{"update_id": 700, "message": MESSAGE},
                                         {"update_id": 701, "message": photo_msg}]))])
    out = tg.get_updates(offset=700, timeout=50, allowed_updates=["message"])

    assert out["ok"] is True
    assert out["next_offset"] == 702  # derived: max(update_id) + 1
    first = out["updates"][0]["message"]
    # Telegram's own names, nothing renamed (C1)
    assert first["message_id"] == 412 and first["chat"]["id"] == 55501
    assert first["from"]["username"] == "zarkone"
    assert first["text"] == "what did I say about the cows?"
    # a photo is a size ladder — the largest is the one worth downloading
    assert out["updates"][1]["message"]["photo"] == {
        "file_id": "big", "file_unique_id": "u2", "width": 1280,
        "height": 853, "file_size": 98000}
    assert out["updates"][1]["message"]["caption"] == "the herd"
    body = fake.requests[0]["json"]
    assert body == {"offset": 700, "timeout": 50, "allowed_updates": ["message"]}
    # a long poll must not time out inside the http effect
    assert fake.requests[0]["timeout"] > 50


def test_get_updates_empty_batch_keeps_the_offset_and_clamps_the_poll():
    tg, fake = load([("/getUpdates", ok([]))])
    out = tg.get_updates(offset=702, timeout=9000)
    assert out == {"ok": True, "updates": [], "next_offset": 702}
    assert fake.requests[0]["json"]["timeout"] == 50  # Telegram's ceiling


def test_webhook_conflict_names_the_fix():
    tg, _ = load([("/getUpdates", err(409, "Conflict: can't use getUpdates method "
                                           "while webhook is active"))])
    out = tg.get_updates()
    assert out["ok"] is False and out["error_code"] == 409
    assert "delete_webhook()" in out["error"]


# ---- writing: sendMessage ---------------------------------------------------

def test_send_message_maps_the_body_and_returns_the_sent_message():
    sent = dict(MESSAGE, message_id=413, text="in the trace, or it didn't happen",
                from_={"id": 8100123})
    sent.pop("from_")
    tg, fake = load([("/sendMessage", ok(sent))])
    out = tg.send_message(55501, "in the trace, or it didn't happen",
                          reply_to_message_id=412)
    assert out["ok"] is True
    assert out["message"]["message_id"] == 413
    assert out["message"]["chat"]["id"] == 55501
    body = fake.requests[0]["json"]
    # unset options are dropped, not sent as nulls
    assert body == {"chat_id": 55501, "text": "in the trace, or it didn't happen",
                    "reply_to_message_id": 412}


def test_send_message_refuses_locally_before_spending_a_call():
    tg, fake = load([])
    assert tg.send_message(None, "hi")["ok"] is False
    assert tg.send_message(55501, "")["ok"] is False
    long = tg.send_message(55501, "x" * 5000)
    assert long["ok"] is False and "4096" in long["error"]
    assert fake.requests == []  # nothing reached Telegram


def test_api_error_carries_telegram_description():
    tg, _ = load([("/sendMessage", err(400, "Bad Request: chat not found"))])
    out = tg.send_message(1, "hello")
    assert out["ok"] is False and out["error_code"] == 400
    assert "chat not found" in out["error"]


def test_rejected_token_says_how_to_re_seed():
    tg, _ = load([("/getMe", err(401, "Unauthorized"))])
    out = tg.me()
    assert out["ok"] is False and out["error_code"] == 401
    assert "connector.key.telegram" in out["error"]


def test_rate_limit_honours_retry_after_then_succeeds():
    tg, fake = load([("/sendMessage", [
        err(429, "Too Many Requests: retry after 7", parameters={"retry_after": 7}),
        ok(MESSAGE)])])
    out = tg.send_message(55501, "hi")
    assert out["ok"] is True
    assert fake.slept == [7]


def test_short_writes_return_a_bare_ok():
    tg, _ = load([("/deleteMessage", ok(True)), ("/sendChatAction", ok(True))])
    assert tg.delete_message(55501, 412) == {"ok": True}
    assert tg.send_chat_action(55501) == {"ok": True}


def test_caption_cap_is_refused_locally():
    tg, fake = load([])
    out = tg.send_photo(55501, "https://example.test/cow.jpg", caption="x" * 2000)
    assert out["ok"] is False and "1024" in out["error"]
    assert fake.requests == []


# ---- files ------------------------------------------------------------------

def test_download_file_is_two_authenticated_hops_returning_a_blob():
    blob_ref = {"__blob": "a" * 64, "bytes": 98000, "mime": "image/jpeg"}
    tg, fake = load([
        ("/getFile", ok({"file_id": "big", "file_path": "photos/file_7.jpg",
                         "file_size": 98000})),
        ("/file/bot", {"status": 200, "headers": {}, "body": blob_ref, "url": ""}),
    ])
    out = tg.download_file("big")
    assert out["ok"] is True
    assert out["file_path"] == "photos/file_7.jpg"
    assert out["file_size"] == 98000
    assert out["blob"].sha256 == "a" * 64 and out["blob"].mime == "image/jpeg"
    # the download hop is a GET on the file base, same marker, same ref
    dl = fake.requests[1]
    assert dl["verb"] == "get"
    assert dl["url"] == ("https://api.telegram.org/file/bot{credential}"
                         "/photos/file_7.jpg")
    assert dl["credential"] == fake.requests[0]["credential"]


def test_download_file_expired_path_is_actionable():
    tg, _ = load([
        ("/getFile", ok({"file_id": "big", "file_path": "photos/file_7.jpg"})),
        ("/file/bot", {"status": 404, "headers": {}, "body": "", "url": ""}),
    ])
    out = tg.download_file("big")
    assert out["ok"] is False and "call download_file again" in out["error"]


# ---- escape hatch -----------------------------------------------------------

def test_raw_posts_any_method_and_guards_the_name():
    tg, fake = load([("/sendPoll", ok({"message_id": 900}))])
    assert tg.raw("sendPoll", {"chat_id": 1, "question": "?",
                               "options": ["a", "b"]})["result"]["message_id"] == 900
    assert fake.requests[0]["url"] == API + "/sendPoll"
    # no path traversal off the pinned host, no non-dict payload
    assert tg.raw("../../evil")["ok"] is False
    assert tg.raw("sendPoll", "chat_id=1")["ok"] is False


def test_raw_truncates_a_huge_result():
    tg, _ = load([("/getChatAdministrators", ok([{"pad": "x" * 500}] * 100))])
    out = tg.raw("getChatAdministrators", {"chat_id": 1})
    assert out["ok"] is True and "result" not in out
    assert "truncated" in out["note"] and len(out["text"]) < 21000
