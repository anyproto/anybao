"""Brain heartbeat v3: v2, and noticing the replies the user sent from Gmail."""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-037. The same brain-heartbeat record (the setup run points it here,
# keeping its `enabled`); everything v2 does (ADR-036 §4), run by v2's own
# `run`, plus one pass before drafting. For each approved approval with no
# `sends` row it looks for the user's message after the approval — in the
# draft's thread, then among messages to its recipients (Open as new
# message may start a new thread) — and on a match writes one sends row,
# consumes the approval and marks the card sent, silently. Seven days
# without one open a send_check card, once. A recipient kept out of the
# brain now means no mail is read for that approval. Nothing here sends:
# the user does, from Gmail.

import re

hb = use("brainHeartbeat@v2")  # noqa: F821 - guest global; the same repo
kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

SUBJECT = "connectors:brainHeartbeat@v3"
PER_RUN = 50               # approvals checked per run: at most two mail queries each
SEARCH_ROWS = 20
MATCH_CHARS = 200
BODY_CHARS = 20000
SEND_CHECK_DAYS = 7
DAY = 86400

_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


def match_text(text):
    """Text as the match compares it: quoted lines dropped, markdown
    backslash escapes removed, whitespace collapsed, case folded."""
    return kit.normalize_ws(_ESCAPE.sub(r"\1", kit.clean_body(text, BODY_CHARS))).casefold()


def is_sent_reply(row, own_address, approved_body, approved_ms):
    """True when `row` is the user's message after the approval carrying the
    approved text's first MATCH_CHARS characters (ADR-037 §3)."""
    needle = match_text(approved_body)[:MATCH_CHARS]
    return (bool(needle) and hb.sender_of(row) == own_address
            and (row.get("internalDate") or 0) > approved_ms
            and needle in match_text(row.get("body")))


def _one(r, dataset, rid):
    rows = _any.query(r.space, r.hub, r.ds[dataset], filter={"id": f"{r.hub}/{rid}"}, limit=1)
    return rows[0] if rows else None


def _approved_at(approval):
    return ts_s(approval.get("decidedAt")) or ts_s(approval.get("modifiedAt"))  # noqa: F821


def _find(r, draft, own, approved_ms, excl, block):
    """The user's sent reply for a draft, or None: its thread first, then any
    message to its recipients; a message carrying someone kept out is skipped
    before its body is read."""
    box = draft.get("sender")
    after = {"$gt": approved_ms}
    filters = []
    if draft.get("threadId"):
        filters.append({"threadId": draft["threadId"], "internalDate": after})
    if draft.get("recipients"):
        filters.append({"participants": {"$in": list(draft["recipients"])},
                        "internalDate": after})
    for flt in filters:
        for row in _any.query(r.space, box, "email_messages", filter=flt,
                              sort=["-internalDate"], limit=SEARCH_ROWS):
            if kit.participant_kept_out(row, excl, block):
                continue
            if is_sent_reply(row, own, draft.get("body"), approved_ms):
                return row
    return None


def _set(r, dataset, rid, ops, what):
    kit.checked(_any.modify(r.space, {"objectId": r.hub, "dataset": r.ds[dataset], "records": [
        {"id": rid, "ops": [{"type": "$set", "path": p, "value": v} for p, v in ops]}]}), what)


def _close(r, aid, card_id, card_status):
    """The approval consumed, its card sent, an open check card answered."""
    _set(r, "approvals", aid, [("status", "consumed")], "consuming the approval")
    if card_id and card_status != "sent":
        _set(r, "cards", card_id, [("status", "sent")], "marking the card sent")
    check = hb.card_id("send_check", aid)
    row = _one(r, "cards", check)
    if row is not None and row.get("status") in hb.OPEN:
        _set(r, "cards", check, [("status", "answered")], "answering the send check")


def _send_check(r, approval, draft, card, approved_at):
    aid = kit.plain_id(approval["id"])
    cid = hb.card_id("send_check", aid)
    if r.card_exists(cid):
        return
    days = int((r.tick - approved_at) // DAY)
    reason = f"approved {days} days ago; no sent reply found in your mail"
    original = kit.plain_id((card or {}).get("id") or approval.get("cardId") or "")
    sid = r.signal("send_check", r.link("approvals", aid), "card", reason, cid)
    r.add_card(cid, {"kind": "send_check",
                     "title": f"Did you send this reply? {draft.get('subject') or ''}".strip(),
                     "reason": reason, "subjectKey": aid, "approvalId": aid,
                     "draftId": kit.plain_id(draft["id"]),
                     "sourceUri": r.link("cards", original), "signalId": sid,
                     "personIds": list((card or {}).get("personIds") or [])})
    r.counts["sendChecks"] = r.counts.get("sendChecks", 0) + 1


def _settle(r, approval, have_send, excl, block):
    """One approved approval: detected, completed, checked, or left."""
    aid = kit.plain_id(approval["id"])
    draft = _one(r, "drafts", approval.get("draftId")) if approval.get("draftId") else None
    if draft is None:
        return
    card = _one(r, "cards", approval.get("cardId") or draft.get("cardId") or "")
    card_id = kit.plain_id(card["id"]) if card else None
    if have_send:
        # marked by hand, half written: finish it, write no second row
        _close(r, aid, card_id, (card or {}).get("status"))
        return
    recipients = [str(x).lower() for x in draft.get("recipients") or []]
    if any(kit.kept_out(x, excl, block) for x in recipients):
        return                          # someone kept out: no mail is read for it
    approved_at = _approved_at(approval)
    own = r.boxes.get(draft.get("sender")) or str(draft.get("senderAddress") or "").lower()
    if approved_at is None or not own:
        return
    row = _find(r, draft, own, int(approved_at * 1000), excl, block)
    if row is not None:
        mid = kit.plain_id(row["id"])
        kit.upsert(r.space, r.hub, r.ds["sends"], aid, {
            "approvalId": aid, "cardId": card_id or approval.get("cardId"),
            "threadId": row.get("threadId") or draft.get("threadId") or mid,
            "messageId": mid, "how": "detected", "at": instant(r.tick)},  # noqa: F821
            "writing the send")
        _close(r, aid, card_id, (card or {}).get("status"))
        r.signal("reply_sent", kit.mail_link(r.space, draft.get("sender"), mid), "none",
                 "sent from Gmail, detected", card_id)
        r.counts["sent"] = r.counts.get("sent", 0) + 1
    elif r.tick - approved_at >= SEND_CHECK_DAYS * DAY:
        _send_check(r, approval, draft, card, approved_at)


def detect_sends(r, excl, block):
    """The sent-detection pass (ADR-037 §3, §5), on v2's run `r`."""
    if not all(r.ds.get(k) for k in ("approvals", "drafts", "sends")):
        return
    approvals = _any.query(r.space, r.hub, r.ds["approvals"], filter={"status": "approved"},
                           sort=["decidedAt"], limit=PER_RUN)
    if not approvals:
        return
    ids = [kit.plain_id(a["id"]) for a in approvals]
    sent = {kit.plain_id(x["id"]) for x in _any.query(
        r.space, r.hub, r.ds["sends"], filter={"id": {"$in": [f"{r.hub}/{i}" for i in ids]}},
        limit=len(ids))}
    for approval in approvals:
        _settle(r, approval, kit.plain_id(approval["id"]) in sent, excl, block)
    for key, line in (("sent", "{} replies sent"), ("sendChecks", "{} send checks")):
        if r.counts.get(key):
            r.extra_did.append(line.format(r.counts[key]))


def main(args):
    return hb.run(args, passes=(detect_sends,), subject=SUBJECT)
