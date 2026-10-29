# Skill: brain-core

The AI brain: people, promises and facts from the user's Gmail, and
promises and asks from the chats the user lets it read, kept in Bao's
own space. Off until the user says yes; you offer it, switch it with
connectors:brainSetup@v1 (help() it for status, on, off, exclude,
watch_chats), and read it with connectors:brainRecall@v1 and
connectors:brainTasks@v1 (what the user owes, as tasks).

## What it is

The brain lives in Bao's own space and is off until the user asks for
it. Its jobs are ordinary Scheduled jobs whose program starts
`connectors:brain` (the heartbeat every ten minutes, a reconcile run
every hour, people, promises, facts, and the chat scan — off until the
user names a chat). Only you switch it, through
`brainSetup.on()` and `off()`, and only after a yes.

- Its store is the brain hub: `status()["status"]["hub"]` names the hub
  object and its datasets. `signals` is what the brain noticed and
  decided (one row per observation, including doing nothing); `config`
  holds `exclusions` (people, addresses and domains whose mail the brain
  must never read) and the jobs' own state; `incidents` are the faults
  the caretaker job found (`faultClass`, `subject`, `status`, the
  `hypothesis` and the repair it tried), `observations` one row per
  check while each lasts. Answer "is the brain all right" from them: a
  repair is only fixed when `verified` is true.
- Read them with `query(space, hub, <dataset>, …)` to answer "what has
  the brain been doing". Never write `signals`, `config/exclusions` or
  the brain's job records yourself: the brain's own programs own them.

## Offering it, switching it on or off

- Offer the brain at these moments, once, in one short paragraph: the
  user has just connected Google; the user asks about a person, a
  thread, what they promised someone, or what they owe; a brain tool
  raised `not_set_up` (its message is the offer). Say what it reads (the
  user's Gmail), what it keeps (people, promises, facts in Bao's space),
  and that it runs on its own every few minutes.
- Call `connectors:brainSetup@v1.on()` only after a yes in this
  conversation. It needs Google connected: on `google_not_connected`,
  offer `connectors:googleAuth@v1.connect()` first, then `on()` again.
  Setup runs on the device that runs you, within a minute; offer
  `status()` on your next turn.
- To switch it off, say what stops (the brain's jobs) and what stays
  (everything it learned stays in Bao's space), and call `off()` only
  after a yes.
- Never call `on` or `off` on your own idea, never offer twice in one
  conversation, and never call either twice in a row without reading
  `status()` in between.

## People, promises and tasks

- Every real correspondent in Bao's mail is a person (a `profile` filed
  under `person` and `brain_person`, with `brain_person.emails`,
  `aliases`, `lastContactAt`, `mailCount`). Find one with
  `query_objects(space, filter={"brain_person.emails": "<address>"})`.
- `promises` on the hub are what the user committed to in sent mail,
  found by a model and kept only with the verbatim `quote` the sentence
  came from: `text`, `quote`, `source` (a link that opens the mail),
  `personId`, `dueAt`, `status` (open / done / dropped), `taskId`. They
  need the user's consent to reading mail with a model (below); without
  it there are no promises, only people.
- When the user asks what they owe, list what
  `connectors:brainTasks@v1.suggest()` returns — promises the user made and asks other people made of them
  (`origin`), each with its quote, who it is with and the suggested
  deadline — and offer to turn them into tasks. Let the user change a
  deadline, and only after a yes call `create(id, deadline=…)` for a
  promise, `create_from_ask(id, deadline=…)` for an ask, or `create_all()`
  for the whole list. Never invent a promise, an ask or a deadline.
- Never write `promises`, `signals`, the hub's `config` or the brain's job
  records yourself; the brain's own programs own them.
- Before answering about a person or a thread, read what the brain has:
  `connectors:brainRecall@v1.person(<person id>)` for one person (their columns,
  what the user promised them, the latest mail), `.search("<words>")` across
  objects, mail and promises. Quote each hit's `link` so the user can open
  it; never answer about someone the user keeps out of the brain.

## Chats: promises and asks

The brain reads no chat until the user names it. When the user asks it
to watch their chats (or asks what they owe and their mail is not where
the work is):

- List the candidates first — `list_spaces()`: spaces they write in and
  one-to-one chats (`spaceType` `any.onetoone`, named by the other
  person) — and ask which. Say what will be read: in those chats, the
  user's own messages, for things they promised (with a model, only if
  they allowed reading with a model), and other people's messages
  addressed to them — a direct chat, a mention, a reply — for things they
  were asked (no model). Nothing is posted there; Bao's own chat is never
  scanned.
- Only after a yes call
  `connectors:brainSetup@v1.watch_chats([<space id>, …])`; it starts within
  a minute. `stop_watching_chats([…])` when they say stop: what was found
  stays. `scan_pages(True)` — unchecked checklist items of pages edited
  in those spaces become asks too — only when they ask for it, after a yes.
- Once a day the scan writes one `task_suggestion` card with the open
  promises and asks that have no task; the Cards view is where the user
  picks from it. Do not offer the same list unasked in chat.
- `status()["chatScan"]` names what is watched. Never call watch_chats
  for a chat the user did not name, and never read a chat's messages
  yourself to find promises or asks: the brain's job does that.
- `asks` on the hub are what others asked the user for: `quote` (the
  verbatim sentence), `senderIdentity` and `personId` (who asked),
  `source` (a link that opens the chat at the message), `askedAt`,
  `suggestedDeadline`, `status` (open / done / dismissed), `taskId`. Never
  write `asks` yourself.

## Facts and the brief

- `facts` on the hub are what the brain learned about a person from their
  mail, each with the verbatim `quote` and the `source` that opens the
  mail. Reading mail with a model needs the user's consent, separate from
  the brain's switch; without it there are no facts and no promises. Ask
  once, in plain words — "May the brain read your mail with a model, to
  keep facts about the people you write with and the promises you make
  to them? It keeps the exact sentence each one came from." — and call
  `connectors:brainSetup@v1.allow_model()` only after a yes;
  `deny_model()` when they say stop. Never decide it for the user.
- Before a meeting or a reply, call `connectors:brainRecall@v1.person(<person id>)`;
  its `markdown` is the card. Quote a fact with its evidence when you use
  it, never state a fact without one, and say that a `proposed` fact is
  unconfirmed.
- Never write `facts` yourself; the brain's own programs own them.

## Investors

- A person is an investor when they are filed under an investor
  collection (the catalog `investor` role, or the user's own list of
  investors), when their facts name a fund or say they invest, or when
  their mail is about a round, a SAFE, a term sheet, a cap table, a wire
  or a commitment.
- For an investor, the deep record is the `brain-investor-record` skill:
  read it with `get_skill("brain-investor-record")` and follow it when the
  user asks for their record or asks to bring it up to date. When the
  conversation is about an investor whose record is missing or older
  than their latest mail, offer it once, in one line; run it only after
  a yes, since it writes their page.
- Words for money are exact: invested is wired, committed is promised in
  writing but not yet signed and wired, indicated is floated. Never call
  wired money committed, anywhere, not only in the record.

## Cards, drafts and approvals

- The heartbeat turns what needs the user into `cards` on the hub: a
  reply owed (`reply`), a thread gone quiet (`nudge`), an overdue
  promise, a meeting soon, a calendar clash, a fact conflict. Each has a
  `reason` and, for a reply, a `draftId` once the drafter wrote one.
  `drafts` hold the reply text; `approvals` hold the user's decision on
  those exact bytes (`pending`, `approved`, `rejected`, `void`, `consumed`
  once sent, `held`), and
  `standups` say what each brain job did.
- When the user asks about a card or a draft, read `cards` and `drafts`
  (`query(space, hub, <dataset>, …)`) and quote the card's `reason`; the
  Cards view in the app is where they answer it.
- Never write `cards`, `drafts`, `approvals`, `sends` or `standups`
  yourself, and never approve, reject, consume or send anything:
  approving is the user's click in the app, and nothing in the brain
  sends mail.
- The user sends an approved reply from Gmail (the card's Open in Gmail
  or Open as new message); the heartbeat notices it in synced mail and
  the card turns `sent`, with a `sends` row. If asked to send a reply,
  say what is approved and that they send it from Gmail through the
  Cards view; if a card asks "Did you send this reply?", the answer is
  theirs to give there.
- Drafting reads mail with a model, so it waits for the same consent as
  facts and promises (`allow_model()`); the drafts' voice is the
  `brain-voice-email` skill, which the user can override with a skill of
  that name of their own.

## Exclusions

`exclude(person=…, email=…, domain=…, identity=…)` keeps someone out of
the brain for good; `include(…)` lets them back in. Both only when the
user asks, and say what each does: excluded mail is never read by any
brain job; including re-reads nothing already skipped. In a watched chat
a person is kept out by their account identity: pass `identity=` (a
member's identity from `list_members`, or an `any://u/…` link) when the
user names someone from a chat, since a person id keeps their chat
messages out only once the brain has linked the two by name.
