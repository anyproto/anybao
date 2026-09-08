"""Telegram Bot API connector — your bot's updates in, messages out.

Long-poll getUpdates for incoming messages (cursor: next_offset),
send/edit/delete text, photos and documents, typing indicators, chat
lookups, file downloads (download_file → a Blob). Anything else:
raw(). Fields keep the Bot API's own names (message_id, chat.id,
file_id). Methods return {ok, ...} or {ok: False, error} — a missing
or revoked token explains how to connect, never a traceback. A bot
sees only group messages addressed to it unless privacy mode is off."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The bot token never enters the guest. Telegram has no header auth
# (tdlib/telegram-bot-api#138) — the token IS a path segment — so every
# request names `credential: {ref: "connector.key.telegram", in: "url"}`
# and writes `{credential}` where the value belongs; the host
# substitutes it after recording and scrubs it back out of everything
# the response echoes (anybao ADR-008 §1). Trim, don't rename: kept
# fields carry the Bot API's own names so the model's knowledge of the
# API transfers; the only invented key is `next_offset`, which the API
# has no scalar for and a poll loop cannot work without.

import json

_HOST = "https://api.telegram.org"
_API = _HOST + "/bot{credential}"        # host-substituted (ADR-008 §1)
_FILES = _HOST + "/file/bot{credential}"
_TOKEN_URL = "https://t.me/BotFather"
_TOKEN_NOTE = ("Talk to @BotFather: /newbot (or /token for an existing one). "
               "The token looks like 8100000000:AAH… — it is the whole "
               "credential, so treat it like a password. For a bot that must "
               "read every group message, /setprivacy -> Disable.")
# `about` is the credential descriptor (ADR-021 §1): what the host
# shows the human when the token is missing or rejected.
_CRED = {"ref": "connector.key.telegram", "in": "url",
         "about": {"label": "Telegram bot token",
                   "hosts": ["api.telegram.org"], "help": _TOKEN_URL,
                   "note": _TOKEN_NOTE}}
_TIMEOUT_S = 60
_MAX_RETRIES = 3
_TEXT_CAP = 4096      # Telegram's own sendMessage cap
_CAPTION_CAP = 1024   # …and its caption cap
_POLL_MAX_S = 50      # long-poll ceiling; the http timeout adds headroom

# ADR-021: the host posts a credential prompt into the chat when the
# ref is missing; the human enters the token there (or in Credentials).
ENTER_HINT = ("enter it in the credential prompt bao posted in the chat, or in "
              "Credentials in the app (CLI: a .connectors.env beside anybao.toml)")

_NOT_CONNECTED = (
    "Telegram not connected — create a bot token at " + _TOKEN_URL + " ("
    + _TOKEN_NOTE + ") then " + ENTER_HINT + " as connector.key.telegram."
)


def _trim(text, cap):
    if not isinstance(text, str) or len(text) <= cap:
        return text
    return text[:cap] + f"… [trimmed, {len(text)} chars]"


def _call(method, body=None, timeout=None):
    """One Bot API call → {ok, result} | {ok: False, error, error_code?}.

    Telegram answers every call with its own {ok, result} / {ok, error_code,
    description} envelope, and 429 carries parameters.retry_after."""
    kw = {"credential": _CRED, "timeout": timeout or _TIMEOUT_S}
    if body:
        kw["json"] = {k: v for k, v in body.items() if v is not None}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = http.post(f"{_API}/{method}", **kw)  # noqa: F821 - guest global
        except EffectError as e:  # noqa: F821 - guest global
            if "no secret for credential ref" in str(e):
                return {"ok": False, "error": _NOT_CONNECTED}
            return {"ok": False, "error": f"request failed: {e}"}
        try:
            b = resp.json()
        except ValueError:
            b = None
        desc = (b or {}).get("description") or f"HTTP {resp.status}"
        if resp.status == 429 and attempt < _MAX_RETRIES:
            wait = ((b or {}).get("parameters") or {}).get("retry_after") or 2
            effect("sleep", {"seconds": min(max(int(wait), 1), 60)})  # noqa: F821
            continue
        if resp.status == 401:
            return {"ok": False, "error_code": 401,
                    "error": "Telegram rejected the bot token (401). Get a fresh one "
                             f"from {_TOKEN_URL} (/token) and re-seed "
                             "connector.key.telegram — " + ENTER_HINT + "."}
        if resp.status == 409:
            return {"ok": False, "error_code": 409,
                    "error": f"Telegram conflict (409): {desc}. getUpdates and a webhook "
                             "are exclusive — call delete_webhook(), or stop the other "
                             "process polling this bot."}
        if resp.status >= 300 or not (b or {}).get("ok"):
            return {"ok": False, "error_code": (b or {}).get("error_code", resp.status),
                    "error": f"Telegram {method} failed: {desc}"}
        return {"ok": True, "result": b.get("result")}
    return {"ok": False, "error_code": 429,
            "error": f"Telegram rate limit on {method} — gave up after "
                     f"{_MAX_RETRIES} retries."}


def _user(u):
    if not isinstance(u, dict):
        return u
    out = {"id": u.get("id"), "is_bot": u.get("is_bot"),
           "first_name": u.get("first_name")}
    for k in ("last_name", "username", "language_code"):
        if u.get(k) is not None:
            out[k] = u[k]
    return out


def _chat(c):
    if not isinstance(c, dict):
        return c
    out = {"id": c.get("id"), "type": c.get("type")}
    for k in ("title", "username", "first_name", "last_name"):
        if c.get(k) is not None:
            out[k] = c[k]
    return out


def _media(m):
    """A file-bearing field trimmed to what download_file needs. `photo`
    is a size ladder — the last entry is the largest."""
    if isinstance(m, list):
        return _media(m[-1]) if m else m
    if not isinstance(m, dict):
        return m
    out = {}
    for k in ("file_id", "file_unique_id", "file_name", "mime_type", "file_size",
              "width", "height", "duration"):
        if m.get(k) is not None:
            out[k] = m[k]
    return out


_MEDIA_KEYS = ("photo", "document", "audio", "voice", "video", "video_note",
               "animation", "sticker")


def _message(m):
    """A Message trimmed to the fields a reader acts on — ids, who, where,
    text, and any file_id worth downloading."""
    if not isinstance(m, dict):
        return m
    out = {"message_id": m.get("message_id"), "date": m.get("date")}
    if isinstance(m.get("chat"), dict):
        out["chat"] = _chat(m["chat"])
    if isinstance(m.get("from"), dict):
        out["from"] = _user(m["from"])
    for k in ("text", "caption"):
        if m.get(k) is not None:
            out[k] = _trim(m[k], _TEXT_CAP)
    for k in ("message_thread_id", "media_group_id", "edit_date"):
        if m.get(k) is not None:
            out[k] = m[k]
    if isinstance(m.get("reply_to_message"), dict):
        r = m["reply_to_message"]
        out["reply_to_message"] = {"message_id": r.get("message_id"),
                                   "text": _trim(r.get("text") or "", 400)}
    for k in _MEDIA_KEYS:
        if m.get(k) is not None:
            out[k] = _media(m[k])
    return out


_UPDATE_MESSAGE_KEYS = ("message", "edited_message", "channel_post",
                        "edited_channel_post")


def _update(u):
    if not isinstance(u, dict):
        return u
    out = {"update_id": u.get("update_id")}
    for k, v in u.items():
        if k == "update_id":
            continue
        out[k] = _message(v) if k in _UPDATE_MESSAGE_KEYS else v
    return out


@span("telegram.me", kind="getter")  # noqa: F821 - guest global
def me():
    """The bot's own identity — the cheapest connectivity check.

    Returns `{ok, user}` — `{id, is_bot, first_name, username,
    can_join_groups, can_read_all_group_messages, ...}`.
    `can_read_all_group_messages: false` is privacy mode: in groups the
    bot only sees /commands and replies to itself (@BotFather ->
    /setprivacy -> Disable, then re-add it to the group)."""
    r = _call("getMe")
    return r if not r["ok"] else {"ok": True, "user": r["result"]}


@span("telegram.get_updates", kind="getter")  # noqa: F821 - guest global
def get_updates(offset=None, limit=None, timeout=None, allowed_updates=None):
    """Incoming updates, oldest first — the polling read.

    `offset`: the first update_id to fetch; pass back the `next_offset`
    of the previous call. Doing so CONFIRMS everything below it —
    Telegram drops those updates and they can never be fetched again,
    so store next_offset before you act on the batch. Without an offset
    you get the unconfirmed backlog (kept ~24h) every time.
    `timeout`: long-poll seconds (0 = return now, max 50) — one call
    per minute at timeout=50 is near-realtime and cheap.
    `allowed_updates`: e.g. ["message"] to skip the rest.

    Returns `{ok, updates, next_offset}` — next_offset is derived
    (max update_id + 1, or the offset you passed when the batch is
    empty). A webhook makes this 409: delete_webhook() first."""
    poll = None
    if timeout is not None:
        poll = min(max(int(timeout), 0), _POLL_MAX_S)
    r = _call("getUpdates",
              {"offset": offset, "limit": limit, "timeout": poll,
               "allowed_updates": allowed_updates},
              timeout=_TIMEOUT_S + (poll or 0))
    if not r["ok"]:
        return r
    updates = r["result"] or []
    ids = [u.get("update_id") for u in updates if isinstance(u.get("update_id"), int)]
    return {"ok": True, "updates": [_update(u) for u in updates],
            "next_offset": max(ids) + 1 if ids else offset}


@span("telegram.send_message", kind="mutator")  # noqa: F821 - guest global
def send_message(chat_id, text, parse_mode=None, reply_to_message_id=None,
                 disable_notification=None, link_preview_options=None,
                 message_thread_id=None):
    """Send a text message → `{ok, message}` (the sent Message).

    `chat_id`: the numeric id from an update's `chat.id`, or
    "@channelusername". A bot cannot open a conversation — the human
    must have messaged it (or added it to the group) first, else
    "chat not found" / "bot was blocked by the user".
    `parse_mode`: None (default, plain text — the safe choice for model
    output, which is full of stray _ and *), "HTML" or "MarkdownV2";
    both require escaping and answer 400 "can't parse entities" when
    it's wrong. `link_preview_options={"is_disabled": True}` kills the
    preview. Text caps at 4096 chars — split longer replies yourself,
    Telegram rejects the whole message otherwise."""
    if not chat_id and chat_id != 0:
        return {"ok": False, "error": "chat_id is required (an update's chat.id, "
                                      "or \"@channelusername\")"}
    if not isinstance(text, str) or not text:
        return {"ok": False, "error": "text is required (a non-empty string)"}
    if len(text) > _TEXT_CAP:
        return {"ok": False,
                "error": f"text is {len(text)} chars; Telegram caps a message at "
                         f"{_TEXT_CAP} — send it as several messages."}
    r = _call("sendMessage",
              {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
               "reply_to_message_id": reply_to_message_id,
               "disable_notification": disable_notification,
               "link_preview_options": link_preview_options,
               "message_thread_id": message_thread_id})
    return r if not r["ok"] else {"ok": True, "message": _message(r["result"])}


@span("telegram.edit_message_text", kind="mutator")  # noqa: F821 - guest global
def edit_message_text(chat_id, message_id, text, parse_mode=None,
                      link_preview_options=None):
    """Rewrite a message the bot itself sent → `{ok, message}`.

    Editing to identical text is a 400 ("message is not modified"), and
    only the bot's own messages are editable."""
    if not isinstance(text, str) or not text:
        return {"ok": False, "error": "text is required (a non-empty string)"}
    if len(text) > _TEXT_CAP:
        return {"ok": False, "error": f"text is {len(text)} chars; the cap is {_TEXT_CAP}"}
    r = _call("editMessageText",
              {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": parse_mode, "link_preview_options": link_preview_options})
    return r if not r["ok"] else {"ok": True, "message": _message(r["result"])}


@span("telegram.delete_message", kind="mutator")  # noqa: F821 - guest global
def delete_message(chat_id, message_id):
    """Delete a message → `{ok}`. The bot's own within 48h; anyone's
    only where it is an admin with delete rights."""
    r = _call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    return r if not r["ok"] else {"ok": True}


@span("telegram.send_chat_action", kind="mutator")  # noqa: F821 - guest global
def send_chat_action(chat_id, action="typing", message_thread_id=None):
    """Show "typing…" (or upload_photo, upload_document, …) → `{ok}`.

    Clears after ~5s or when the next message lands — send it before
    slow work so the human sees the bot is alive."""
    r = _call("sendChatAction", {"chat_id": chat_id, "action": action,
                                 "message_thread_id": message_thread_id})
    return r if not r["ok"] else {"ok": True}


@span("telegram.send_photo", kind="mutator")  # noqa: F821 - guest global
def send_photo(chat_id, photo, caption=None, parse_mode=None,
               disable_notification=None):
    """Send a photo by URL or by a file_id Telegram already holds →
    `{ok, message}`.

    `photo`: an https URL Telegram fetches itself, or a `file_id` from
    an earlier update (re-sending by file_id is free and instant).
    Uploading local bytes is not supported here — Telegram wants
    multipart; host a URL or use raw(). Caption caps at 1024."""
    if caption is not None and len(caption) > _CAPTION_CAP:
        return {"ok": False, "error": f"caption caps at {_CAPTION_CAP} chars"}
    r = _call("sendPhoto",
              {"chat_id": chat_id, "photo": photo, "caption": caption,
               "parse_mode": parse_mode, "disable_notification": disable_notification})
    return r if not r["ok"] else {"ok": True, "message": _message(r["result"])}


@span("telegram.send_document", kind="mutator")  # noqa: F821 - guest global
def send_document(chat_id, document, caption=None, parse_mode=None,
                  disable_notification=None):
    """Send a file by URL or file_id → `{ok, message}`. Same URL /
    file_id rule (and 1024-char caption cap) as send_photo."""
    if caption is not None and len(caption) > _CAPTION_CAP:
        return {"ok": False, "error": f"caption caps at {_CAPTION_CAP} chars"}
    r = _call("sendDocument",
              {"chat_id": chat_id, "document": document, "caption": caption,
               "parse_mode": parse_mode, "disable_notification": disable_notification})
    return r if not r["ok"] else {"ok": True, "message": _message(r["result"])}


@span("telegram.get_chat", kind="getter")  # noqa: F821 - guest global
def get_chat(chat_id):
    """One chat's metadata → `{ok, chat}` — title/username, description,
    pinned message, permissions. The bot must be a member."""
    r = _call("getChat", {"chat_id": chat_id})
    return r if not r["ok"] else {"ok": True, "chat": r["result"]}


@span("telegram.download_file", kind="getter")  # noqa: F821 - guest global
def download_file(file_id):
    """Fetch a file an update pointed at → `{ok, file_path, file_size,
    blob}`.

    `file_id` comes from a message's photo/document/voice/… . Two hops
    (getFile, then the download), both host-authenticated. `blob` is a
    Blob — hand it straight to llm.chat as a file part, or fs.write it.
    Bot API downloads cap at 20 MB."""
    if not file_id:
        return {"ok": False, "error": "file_id is required (from a message's "
                                      "photo/document/voice/... field)"}
    r = _call("getFile", {"file_id": file_id})
    if not r["ok"]:
        return r
    meta = r["result"] or {}
    path = meta.get("file_path")
    if not path:
        return {"ok": False, "error": "Telegram returned no file_path for that file_id"}
    try:
        resp = http.get(f"{_FILES}/{path}", credential=_CRED,  # noqa: F821
                        timeout=_TIMEOUT_S)
    except EffectError as e:  # noqa: F821 - guest global
        return {"ok": False, "error": f"download failed: {e}"}
    if resp.status >= 300:
        return {"ok": False, "error_code": resp.status,
                "error": f"download failed: HTTP {resp.status} (a file_path expires "
                         "after ~1h — call download_file again)"}
    return {"ok": True, "file_path": path, "file_size": meta.get("file_size"),
            "blob": resp.blob}


@span("telegram.get_webhook_info", kind="getter")  # noqa: F821 - guest global
def get_webhook_info():
    """Is a webhook set (and is it healthy)? → `{ok, webhook}` —
    `{url, pending_update_count, last_error_message, ...}`. A non-empty
    `url` is why get_updates answers 409."""
    r = _call("getWebhookInfo")
    return r if not r["ok"] else {"ok": True, "webhook": r["result"]}


@span("telegram.delete_webhook", kind="mutator")  # noqa: F821 - guest global
def delete_webhook(drop_pending_updates=None):
    """Drop the webhook so get_updates works → `{ok}`.

    This takes the bot away from whatever was receiving those webhooks.
    `drop_pending_updates=True` also discards the backlog."""
    r = _call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})
    return r if not r["ok"] else {"ok": True}


@span("telegram.raw", kind="mutator")  # noqa: F821 - guest global
def raw(method, payload=None):
    """Any other Bot API method → `{ok, result}` — the escape hatch.

    `method` is the bare camelCase name ("sendPoll", "pinChatMessage",
    "setMyCommands"); `payload` is its arguments as a dict, posted as
    JSON. Pinned to api.telegram.org — the token never goes anywhere
    else. Big results come back stringified under `text`."""
    if not isinstance(method, str) or not method.isalnum():
        return {"ok": False, "error": "method is the bare Bot API name, letters and "
                                      "digits only (e.g. \"sendPoll\")"}
    if payload is not None and not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be a dict of the method's arguments"}
    r = _call(method, payload or None)
    if not r["ok"]:
        return r
    dumped = json.dumps(r["result"])
    if len(dumped) > 20000:
        return {"ok": True, "text": _trim(dumped, 20000),
                "note": "result truncated — narrow it with the method's own params"}
    return {"ok": True, "result": r["result"]}


def main(args):
    return me()
