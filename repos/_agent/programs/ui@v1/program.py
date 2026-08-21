"""any-ui backend helpers, invoked via serve's POST /run (ADR-009 §6).

main(args) dispatches on args["method"]; the UI calls
{"program": "agent:ui@v1", "args": {"method": ..., ...}}. Methods:
emailSummary — fill one email_messages record's summary via the cheap
LLM tier; {ok, summary, cached?} or {ok: False, error} with NOTHING
written on failure. Not an agent tool — bao has its own surfaces."""

# Space-resident on purpose (user call, 2026-08-21): updating the UI's
# backend logic is a deploy, not an any-ui release. Inline /run
# {source} stays available as the debugging escape hatch.

_any = use("any@v1")  # noqa: F821 - guest global
_llm = use("llm@v1")  # noqa: F821 - guest global

_DATASET = "email_messages"
_BODY_CAP = 12_000     # prompt input cap; bodies are cleaned markdown
_MAX_TOKENS = 256      # a summary is 1-2 sentences, never more

_SYSTEM = (
    "You summarize one email for a mail-list preview. Reply with ONLY "
    "the summary text: 1-2 plain sentences, at most ~50 words, no "
    "preamble, no markdown, no quotes around it. Name the concrete "
    "ask/outcome if there is one; never invent facts not in the email."
)


@span("ui.email_summary", kind="mutator")  # noqa: F821 - guest global
def email_summary(space, mailbox_id, record_id, force=False):
    """Generate + store the summary for one email record.

    `mailbox_id` = the mailbox OBJECT id hosting the dataset,
    `record_id` = the record id (the Gmail message id). The summary
    lands in the record's author-mutable `summary` field (ADR-016 §1,
    deliberately unindexed). An existing summary short-circuits
    ({cached: True}) unless force. Returns `{ok, summary, cached?}`;
    `{ok: False, error}` on a missing record, an LLM failure, or an
    empty model reply — the record is never touched on failure.
    """
    if not space or not mailbox_id or not record_id:
        return {"ok": False,
                "error": "space, mailbox_id and record_id are required"}
    rows = _any.query(space, mailbox_id, _DATASET,
                      filter={"id": {"$in": [record_id]}}, limit=1)
    if not rows:
        return {"ok": False,
                "error": f"record not found: {record_id} in {_DATASET}"}
    rec = rows[0]
    existing = (rec.get("summary") or "").strip()
    if existing and not force:
        return {"ok": True, "summary": existing, "cached": True}

    text = ("Subject: " + (rec.get("subject") or "") + "\n"
            + "From: " + (rec.get("from") or "") + "\n"
            + "Date: " + (rec.get("date") or "") + "\n\n"
            + (rec.get("body") or rec.get("snippet") or "")[:_BODY_CAP])
    try:
        reply = _llm.chat(
            [{"role": "user", "parts": [{"type": "text", "text": text}]}],
            system=_SYSTEM, tier="classify", max_tokens=_MAX_TOKENS)
    except Exception as e:  # LlmError or transport — never a traceback out
        return {"ok": False, "error": f"summary model call failed: {e}"}
    summary = " ".join(
        p.get("text", "") for p in reply.get("parts", [])
        if p.get("type") == "text").strip()
    if not summary:
        return {"ok": False, "error": "model returned no text"}
    _any.upsert_records(space, mailbox_id, _DATASET,
                        [{"id": record_id, "fields": {"summary": summary}}])
    return {"ok": True, "summary": summary}


_METHODS = {"emailSummary": email_summary}


def main(args):
    args = dict(args or {})
    method = args.pop("method", None)
    fn = _METHODS.get(method)
    if not fn:
        return {"ok": False,
                "error": f"unknown method: {method!r} — one of "
                         f"{sorted(_METHODS)}"}
    try:
        return fn(**args)
    except TypeError as e:  # wrong/missing kwargs — actionable, not a traceback
        return {"ok": False, "error": f"bad args for {method}: {e}"}
