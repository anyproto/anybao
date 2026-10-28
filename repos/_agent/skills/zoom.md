# Skill: zoom

Zoom meetings in a space: the easiest flow is a synced Gmail mailbox
plus a small cron program that turns Zoom AI Companion summary emails
into meeting objects. No Zoom app, no API key.

## What Zoom sends

After a meeting, Zoom emails the AI Companion summary from
`no-reply@zoom.us`; the subject is the meeting topic. The host always
gets it; invitees only if the host's "Automatically share summary
with" setting allows it; ad-hoc joiners never. The body carries the
full summary (a recap, next steps, per-topic summaries) ONLY when the
Zoom setting "Include the full summary text in the email" is on —
otherwise it is a sign-in link and there is nothing to convert. The
same sender also sends recording-ready and invitation mail: the
sender alone is not a summary. The email names no attendees; names
appear only inside the prose.

## Setup (one conversation, in this order)

1. Gmail synced into the target space (`gmailSync` skill). The sync
   scope must include `from:no-reply@zoom.us` — a `-from:` exclusion
   or a label-only scope hides the summaries. Ask the user to turn on
   "Include the full summary text in the email" (Zoom web → Settings
   → AI Companion) if it is off.
2. Confirm on ONE real message before writing anything:
   `query(space, mailbox_id, "email_messages",
   filter={"participants": "no-reply@zoom.us"},
   sort=["-internalDate"], limit=5)`. Read a body: a zoom.us link
   plus "Quick recap" / "Next steps" / "Summary" headings = full
   text; a link and no headings = link-only → tell the user the
   setting is off and stop here. Note the exact heading wording you
   see; the program matches on it.
3. Make the `meeting` type if the space has none (c.list_types):
   xKey `meeting`, icon `{"glyph": "video"}`, properties `startTime`
   (datetime), `source` (text: "zoom"), `sourceEmail` (text: the
   email record link), `zoomUrl` (url), `attendees` (text: addresses,
   comma-joined). The summary is the object's body.
4. Write the program with programs@v1.create_program — name
   `zoomMeetings`, a `main(args)` with args `{space, mailboxId}`:
   - cursor: `cfg.get("zoomMeetings.cursor")` (config@v1; an
     `internalDate`, 0 on first run); query records newer than it,
     sorted ascending, limit 50 per run.
   - keep a record only if `from` contains `no-reply@zoom.us`, the
     body has a `zoom.us/` link AND one of the headings from step 2.
   - dedupe: `query_objects(space, filter={"meeting.sourceEmail":
     link})` — Zoom re-sends when the host edits and shares; skip a
     link already used. `link` =
     `any://o/<space>/<mailboxId>/email_messages/<id>`.
   - attendees: `use("connectors:googleCalendar@v1").list_events(
     time_min, time_max, q=subject)` for the email's day ±1; take the
     first event's attendee addresses. No match = leave empty, never
     guess from the prose.
   - create: `c.create_object(space, {"type": "meeting",
     "initialProperties": {"any": {"name": subject}, "meeting":
     {...}}, "markdown": body})`, then advance the cursor to this
     record's `internalDate` (write after each object, not at the
     end).
   - report: one `c.chat_send(bao, c.general_chat(bao), {"text":
     "New meeting: <name> <any://o/… link>", "agent": {"name":
     "trigger:zoom-meetings", "done": True}})` per new meeting;
     silent when nothing is new.
5. Test with a `"once"` trigger at now() + 30, then
   `effects.runs(filter={"triggerId": "zoom-meetings"}, limit=1)`
   (`_core` skill). Never call main from a cell.
6. Schedule it: an agent_triggers record `zoom-meetings`, `"kind":
   "cron"`, `"spec": {"every_s": 900}`, `"program":
   "<spec from create_program>"`, args as above, `enabled: true`.
   Tell the user what runs and how often.

## After a meeting lands

- "What did we decide with X" = `query_objects(space,
  filter={"any.type": "meeting"}, sort=["-meeting.startTime"])`, then
  the body; c.search finds meetings under the basic scope.
- Enrich the space from it on request: `use("agent:enrich@v1")
  .propose(space, meeting_id)`, user reviews, then `apply`. Never
  apply unasked.
- Next steps → reminders (`_core` skill) or calendar events, only
  when the user says so.

## Limits

Link-only emails, E2EE meetings (no summary exists), and ad-hoc
joiners are out of reach by email. Attachments are never synced.
A per-user Zoom API connector would cover link-only accounts and
exact participant lists; it does not exist yet.
