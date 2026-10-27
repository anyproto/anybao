# Skill: brain-core

The AI brain: people, promises and facts from the user's Gmail, kept in
Bao's own space. Off until the user says yes; you offer it, switch it
with connectors:brainSetup@v1 (help() it for status, on, off, exclude),
and read it with connectors:brainRecall@v1 and connectors:brainTasks@v1.

## What it is

The brain lives in Bao's own space and is off until the user asks for
it. Its jobs are ordinary Scheduled jobs whose program starts
`connectors:brain` (the heartbeat every ten minutes, a reconcile run
every hour, people, promises, facts). Only you switch it, through
`brainSetup.on()` and `off()`, and only after a yes.

- Its store is the brain hub: `status()["status"]["hub"]` names the hub
  object and its datasets. `signals` is what the brain noticed and
  decided (one row per observation, including doing nothing); `config`
  holds `exclusions` (people, addresses and domains whose mail the brain
  must never read) and the jobs' own state; `incidents` and
  `observations` are for the caretaker later.
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
- `promises` on the hub are what the user committed to in sent mail:
  `text`, the verbatim `quote`, `source` (a link that opens the mail),
  `personId`, `dueAt`, `status` (open / done / dropped), `taskId`.
- When the user asks what they owe, or at most once a day when
  `connectors:brainTasks@v1.suggest()` returns three or more, offer to turn
  promises into tasks: show each with its suggested deadline, let the user
  change it, and call `create(id, deadline=…)` or `create_all()` only
  after a yes. Never invent a promise or a deadline.
- Never write `promises`, `signals`, the hub's `config` or the brain's job
  records yourself; the brain's own programs own them.
- Before answering about a person or a thread, read what the brain has:
  `connectors:brainRecall@v1.person(<person id>)` for one person (their columns,
  what the user promised them, the latest mail), `.search("<words>")` across
  objects, mail and promises. Quote each hit's `link` so the user can open
  it; never answer about someone the user keeps out of the brain.

## Facts and the brief

- `facts` on the hub are what the brain learned about a person from their
  mail, each with the verbatim `quote` and the `source` that opens the
  mail. Reading mail with a model needs the user's consent, separate from
  the brain's switch; without it there are no new facts. Ask once, in
  plain words — "May the brain read your mail with a model to keep facts
  about the people you write with? It quotes the sentence each fact came
  from." — and call `connectors:brainSetup@v1.allow_model()` only after a
  yes; `deny_model()` when they say stop. Never decide it for the user.
- Before a meeting or a reply, call `connectors:brainRecall@v1.person(<person id>)`;
  its `markdown` is the card. Quote a fact with its evidence when you use
  it, never state a fact without one, and say that a `proposed` fact is
  unconfirmed.
- Never write `facts` yourself; the brain's own programs own them.

## Exclusions

`exclude(person=…, email=…, domain=…)` keeps someone's mail out of the
brain for good; `include(…)` lets them back in. Both only when the user
asks, and say what each does: excluded mail is never read by any brain
job; including re-reads nothing already skipped.
