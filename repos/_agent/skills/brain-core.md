# Skill: brain-core

The AI brain: what it keeps in Bao's own space, what you may and may not
write there, and how to switch it on or off with agent:brainSetup@v1
(help() it for status, on, off, exclude, set_chat_spaces).

## What it is

The brain lives in Bao's own space and is on by default. Its jobs are
ordinary Scheduled jobs whose program starts `agent:brain` (the
heartbeat every ten minutes, a reconcile run every hour). The user
switches it in Settings → Brain, or by asking you.

- Its store is the brain hub: `status()["status"]["hub"]` names the hub
  object and its datasets. `signals` is what the brain noticed and
  decided (one row per observation, including doing nothing); `config`
  holds `exclusions` (people, addresses and domains whose mail the brain
  must never read) and the jobs' own state; `incidents` and
  `observations` are for the caretaker later.
- Read them with `query(space, hub, <dataset>, …)` to answer "what has
  the brain been doing". Never write `signals`, `config/exclusions` or
  the brain's job records yourself: the brain's own programs own them.

## Switching it on or off

- On by default. To switch it off, say what stops (the brain's jobs)
  and what stays (everything it learned stays in Bao's space), and call
  `agent:brainSetup@v1.off()` only after a yes in this conversation.
  Same for `on()`. Then point at Settings → Brain, or offer `status()`
  on your next turn: setup runs on the device that runs you, within a
  minute.
- Never call `on` or `off` on your own idea, and never twice in a row
  without reading `status()` in between.

## People, promises and tasks

- Every real correspondent in Bao's mail is a person (a `profile` filed
  under `person` and `brain_person`, with `brain_person.emails`,
  `aliases`, `lastContactAt`, `mailCount`). Find one with
  `query_objects(space, filter={"brain_person.emails": "<address>"})`.
- `promises` on the hub are what the user committed to in sent mail:
  `text`, the verbatim `quote`, `source` (a link that opens the mail),
  `personId`, `dueAt`, `status` (open / done / dropped), `taskId`.
- When the user asks what they owe, or at most once a day when
  `agent:brainTasks@v1.suggest()` returns three or more, offer to turn
  promises into tasks: show each with its suggested deadline, let the user
  change it, and call `create(id, deadline=…)` or `create_all()` only
  after a yes. Never invent a promise or a deadline.
- Never write `promises`, `signals`, the hub's `config` or the brain's job
  records yourself; the brain's own programs own them.
- Before answering about a person or a thread, read what the brain has:
  `agent:brainRecall@v1.person(<person id>)` for one person (their columns,
  what the user promised them, the latest mail), `.search("<words>")` across
  objects, mail and promises. Quote each hit's `link` so the user can open
  it; never answer about someone the user keeps out of the brain.

## Exclusions

`exclude(person=…, email=…, domain=…)` keeps someone's mail out of the
brain for good. Use it when the user asks you to keep a person out.
You can add an exclusion but never remove one: removing is the user's,
in Settings.
