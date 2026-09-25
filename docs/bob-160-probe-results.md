# BOB-160 probe results (2026-09-25)

49 questions from `docs/bob-160-probe-plan.md` against the cut prompt (`_core` 3793e62 + `_any` a25d33f; "hi" = 9,300 Claude tokens vs 34,684 at baseline), sonnet-5 on the staging-bob160 rig, chat wiped per series, graded from traces.

**35 PASS · 12 PARTIAL · 3 FAIL · 1 invalid probe (7.2) · $1.64 · 104 of 304 turns wasted.**

The cut held for the core loop: object writes, search, trash, links, apps, memory and on-demand skills mostly pass at 2–5 turns and ~$0.01–0.03. The three FAILs and most PARTIALs trace to a few specific holes, below. A third of all turns were wasted, and most of that waste comes from docstrings, error messages and trace tooling, not from the cut skill text. Two infrastructure issues cost more than any cut saved: help() on any@v1 returns an elided stub, and the system-prompt cache misses whenever memory categories or the skills index change.

## 1. Skill lines to add back or fix (the cut went too far, or the old text was wrong)

- **Delete rules conflict.** (2.3, 2.4) _core says list-and-ask before deleting; _any says 'delete this' = trash. The model asked on an explicit 'Delete Seed 30', then 'bring it back' restored the wrong object (Plant 30). The conflict predates the cut.
  Fix: _core: ask before delete_object or bulk deletes; an explicit 'delete X' trashes X now and says restore undoes it.
- **Reading a chat.** (6.2) The only query recipe left in the prompt is _core's chat_log drill-down, and the model copied it onto chat_messages. That returned [] three times.
  Fix: _any chat line: read with c.query(space, c.general_chat(space), "chat_messages", sort=["-createdAt"], limit=n). general_chat returns the id string.
- **Trigger recipe uses an unbound chat_id; cron timezone is not stated.** (8.1, 8.3) The reminder raised a NameError before it succeeded. For 'every morning at 8' the model added a tz key, which is ignored, so it fires at 10:00 local.
  Fix: Recipe: baoSpaceConfig["chatId"]; add 'cron is UTC: convert with tz_offset()'. Runtime: reject unknown spec keys (see group 3).
- **Wiki placement stops at an install question.** (3.1, 2.5) move_object and create_object(parent=) install the wiki themselves, but nothing says so. The Apps line ('offer setup_app on a yes') made the model ask for permission, so no folder was made.
  Fix: _any wiki line: 'placing an object installs the wiki when missing'. Also fix the misleading 'create_collection makes one' error for app-owned collections.
- **Files: where the bytes come from, and keeping the link.** (4.1, 4.2) The model guessed attach_file(url=), then lost the file link between turns.
  Fix: _any files line: attach_file(space, oid, name, http.get(url).blob); put the returned uri in the reply.
- **Devices: switch path not reached.** (5.6) The model called list_devices straight from the pointer without help(), so it never saw the Settings path.
  Fix: _any: '…c.list_devices(); switching is the user's: Settings ▸ Agent ▸ Devices ▸ Use this device, on that device'.
- **Key pasted in chat.** (7.3) The model refused the key but didn't point to Credentials, and offered a card no connector would raise.
  Fix: _core credentials: a key pasted in chat → tell them to delete the message and enter it in Credentials.
- **'Did it run?' read from the trigger record.** (8.2) The effects.runs line was present, but the model read lastRunAt instead.
  Fix: Put the pointer where it is used: the agent_triggers dataset description, or a hint on reading the record.
- **Claims about sources never queried.** (10.3, 10.4, 9.1) 'no mail hits' with no mail query; 'not trashed' with no bin query.
  Fix: _core conduct: never report on a source you didn't query.

## 2. Docstrings and error messages (the model did look; the text failed it)

- **help(c) on any@v1 is useless.** (3.2, 4.3) 7.2 KB output exceeds the inline budget and comes back as a values.get stub. _core tells the model to call help(mod) first, so this costs a turn in many runs.
  Fix: help(module) output for tool programs = the one-line method listing (fits inline); or exempt help() from elision up to a cap.
- **create_type never shows a choice property.** (1.1) The model guessed 'select' plus a list, got a 400, and needed 3 calls.
  Fix: One inline choice example in create_type; the unknown-slug error lists valid slugs.
- **currentUserSpace=None: the TypeError still suggests it.** (9.0, 9.2, 10.2, 10.3, 5.5) Cost a turn plus a list_spaces call in 9 runs. Partly a probe artifact (API messages carry no view), but real for any message sent without a view.
  Fix: When the value is None, say 'currentUserSpace is None (no view): pass the space name'.
- **Inconsistent return shapes.** (6.1, 6.2) general_chat returns a str; chat_log and bundle_child return {objectId}.
  Fix: Make them uniform, or state 'returns the id string' in the first line.
- **Silent empty results.** (6.2, 11.3, 9.3, 9.1) A query on a dataset the object doesn't declare returns []; a select filter with a wrong value matches nothing with no error; runs() startedAt is epoch seconds, but instant() is the documented form (0 rows); effects.of(cell_id=<span id>) returns [].
  Fix: Error or warn on each: dataset.not_declared, unknown option value, normalize instants in runs(), unknown cell id.
- **setup_app docstring names 'person'; the type is 'profile'.** (5.4)
  Fix: Return the installed type and collection keys in the result, and fix the doc.
- **create_skill description replaces the body.** (11.3) The model acted from the index line and never called get_skill.
  Fix: create_skill doc: the description names the trigger, not the procedure.
- **Setup-app consent.** (5.3a) The model saw the full 'offer, then install on a yes' docstring and still installed on 'Add contacts to Garden'.
  Fix: Decide whether an explicit 'add X' is the yes; state it in _any either way.

## 3. Runtime / server bugs (no prompt fixes these)

- **The prompt cache misses on the whole prefix when memory categories or the skills index change.** (9.5, 11.3, 8.4) compose_system ends with 'Memory categories in use', and the Skills index and repo list sit before the tool docs. There is one cache breakpoint, so a first memory in a new category, a new skill or a new program forces a full ~9.3k-token rewrite: ~6× the cost of that turn.
  Fix: Move the changing tails (categories, skills index, repos) after a second cache breakpoint, or after the tool docs.
- **Trace forensics: 21 turns, $0.155 (BOB-172 confirmed).** (9.1, 9.2) Span records hold only the end (no input), cell rows have no code, boot noise leads effects.of, and no view returns a past cell's source.
  Fix: BOB-172: effects.cells(run) with code + paired input/output; boot excluded by default.
- **Cron is UTC, and unknown spec keys (tz) are silently accepted.** (8.3)
  Fix: Validate the spec; optionally support tz.
- **A failed composite create_type leaves a half-made type behind.** (1.1) The 400 call still created the type, and the retry reported created:false.
  Fix: Server: make the composite atomic, or roll back on a property failure.
- **backlinks returns no chat-message edges for any://o links posted in bao's chat.** (3.4) Needs checking: either not indexed, or cross-space edges are dropped.
  Fix: Check upstream.
- **A progress bubble posted inner monologue verbatim.** (7.2)
  Fix: Check how progress narration is chosen.

## 4. Probe caveats

- **7.2 invalid: the rig has a Linear key, so the missing-key path never ran.** (7.2)
  Fix: Re-run with connector.key.linear removed.
- **No UI view: currentUserSpace is always None over the API; the wasted turns above include that.**
- **5.1's unanswered 'which space?' leaked into 5.2–5.3b; the 'yes' went to the Ideas page, so the setup_app-on-yes path wasn't exercised.** (5.1, 5.3b)
- **Fixture drift across series: Plant 1 became a task (1.6), Plant 30 was restored (2.4), Sprout 1–40 were added (8.4).**
- **Rig state left behind: a DAILY cron trigger from 8.3 that fails on every fire, Contacts installed in Garden, a Task type (task_item), the status-check skill, a task-count program, a coin-flip applet, the Exports zip page.** (8.3)

## Every answer

| id | verdict | turns | wasted | $ | cause |
|---|---|---:|---:|---:|---|
| 1.1 | PARTIAL | 11 | 4 | 0.080 | create_type's docstring says nothing about choice properties: no 'choice' slug, no options shape. It only points to add_property's slug tabl… |
| 1.2 | PASS | 5 | 1 | 0.025 | Extra verification query_objects turn after create_object had already returned the resolved status; the dead line `... if False else None` s… |
| 1.3 | PASS | 4 | 0 | 0.016 |  |
| 1.4 | PASS | 4 | 0 | 0.017 |  |
| 1.5 | PASS | 4 | 0 | 0.020 |  |
| 1.6 | PASS | 4 | 0 | 0.018 |  |
| 1.7 | PASS | 2 | 0 | 0.009 |  |
| 2.1 | PASS | 3 | 0 | 0.012 |  |
| 2.2 | PASS | 5 | 0 | 0.021 |  |
| 2.3 | PARTIAL | 3 | 1 | 0.014 | Conflicting guidance: _core Conduct says 'Before deleting, list it and ask', while _any says '"Delete this" = c.trash (reversible with resto… |
| 2.4 | FAIL | 4 | 0 | 0.023 | Cascade from 2.3 (nothing to restore) plus mis-resolving a deictic: 'Actually' reversed the pending 'Trash it?', so the right move was 'Seed… |
| 2.5 | PASS | 5 | 2 | 0.029 | Read 'pages in Garden' as wiki pages: turn 1 queried any.collections='wiki' (error), and turn 2 tried list_children("") (0). _any's Wiki par… |
| 3.1 | FAIL | 10 | 6 | 0.051 | move_object and create_object(parent=…) set up the wiki themselves: _wiki() POSTs /v1/catalog/wiki/setup, which is idempotent. Neither docst… |
| 3.2 | PASS | 6 | 3 | 0.023 | Turn 1 help(c) dumped the whole surface without being used. Turns 2 and 3 re-resolved Garden and Dune ids with list_spaces + search, althoug… |
| 3.3 | PASS | 1 | 0 | 0.004 |  |
| 3.4 | PASS | 3 | 0 | 0.012 |  |
| 4.1 | PASS | 9 | 3 | 0.040 | The _any Files line says 'attach_file the image, then write ![alt](uri)' but not how a URL becomes data, so the model guessed attach_file(ur… |
| 4.2 | PASS | 5 | 2 | 0.023 | Turn 1 built a file link itself with the fileId missing, which the attach_file doc and the _any Links line forbid, because 4.1's reply had n… |
| 4.3 | PASS | 11 | 1 | 0.050 | Turn 1 help(c) returned a 7 KB elided str that was never read, a whole turn of discovery for nothing. With the recipe gone, discovery took 3… |
| 5.1 | PASS | 3 | 1 | 0.009 | Turn 1 was a bare `currentUserSpace` expression. The value None rendered as '(no output)', so turn 2 had to re-run it as print(currentUserSp… |
| 5.2 | PASS | 3 | 0 | 0.011 |  |
| 5.3a | PARTIAL | 5 | 0 | 0.024 | Installed Contacts via setup_app with no offer. The model saw the FULL setup_app docstring (the '+N chars — --full' marker is trace-show dis… |
| 5.3b | PASS | 3 | 0 | 0.013 | Probe-sequence drift: 5.3a skipped the offer, and the pending 5.1 question consumed the 'yes'. |
| 5.4 | PASS | 9 | 3 | 0.041 | The setup_app docstring says 'a contact is a `person` filed under `contact`', but the installed type's xKey is `profile` (`person/v2` is the… |
| 5.5 | PASS | 7 | 2 | 0.040 | The user named Garden, but turn 3 passed currentUserSpace (None), which was a model slip. The error said a space NAME string works, yet turn… |
| 5.6 | PARTIAL | 2 | 0 | 0.008 | The switch path lives only in the list_devices docstring, and the model called the method from the skill pointer without help(). The skill l… |
| 5.7 | PASS | 1 | 0 | 0.004 |  |
| 6.1 | PASS | 5 | 2 | 0.018 | general_chat returns a bare id str but chat_log returns {objectId}. The model guessed the dict shape it had seen elsewhere. The general_chat… |
| 6.2 | PASS | 11 | 8 | 0.050 | The model pattern-matched the _core 'compressed context is drillable' recipe (c.query(space, c.chat_log(space, chat_id)["objectId"], ..., so… |
| 6.3 | PARTIAL | 4 | 3 | 0.016 | The _any 'never chat_send into the chat you are answering in' line did not stop the first instinct on 'Say hi in this chat too'. The model r… |
| 7.1 | PASS | 3 | 1 | 0.010 | Spent a whole turn on help(figma) before figma.me(), although _core already names figma.me() as the method to call; the 'help(mod) in the fi… |
| 7.2 | PARTIAL (invalid probe) | 10 | 2 | 0.058 | Rig setup, not the model: connector.key.linear is set on this rig. Minor model waste: turn 7 printed currentUserSpace (None, already in ui c… |
| 7.3 | PARTIAL | 1 | 0 | 0.004 | _core says 'Keys no call would ask for are managed in the app's **Credentials**' but the model read it as background, not as what to tell th… |
| 8.1 | PASS | 3 | 1 | 0.013 | The _core reminder recipe uses `chat_id`, which is not bound in the kernel (only baoSpaceConfig={spaceId, chatId} and currentUserSpace are).… |
| 8.2 | PARTIAL | 3 | 1 | 0.021 | _core says twice that 'Did it run?' = effects.runs(filter={triggerId}) and 'never a trigger record's fields'. The model ignored it, because … |
| 8.3 | FAIL | 14 | 5 | 0.076 | (1) Neither _core nor the trigger docs say that cron is UTC, and the runtime silently accepts and ignores an unknown 'tz' key. (2) The progr… |
| 8.4 | PARTIAL | 14 | 8 | 0.087 | The model never called help(c.create_object), whose docstring lists the top-level 'name' key, and assumed a markdown H1 sets the title. Noth… |
| 9.0 | PASS | 5 | 2 | 0.019 | currentUserSpace is None in this chat, yet the TypeError lists it as a valid choice ('a bound cell global (currentUserSpace, baoSpaceConfig)… |
| 9.1 | PARTIAL | 21 | 13 | 0.155 | Wasted turns: t3 dumps keys after traceRef was already printed. t6 effects.get on 4 cell seqs returns only phase:end span rows with no input… |
| 9.2 | PASS | 13 | 9 | 0.077 | The trace views give no way to get a past cell's source, so 're-run the cell' meant rebuilding it: t4 outline full of boot getters, t5/t7/t1… |
| 9.3 | PASS | 6 | 2 | 0.039 | The runs() docstring calls startedAt 'an epoch instant' and gives {"$gte": ts}, but the stored value is float epoch SECONDS (endedAt 1790355… |
| 9.4 | PASS | 2 | 0 | 0.008 |  |
| 9.5 | PASS | 1 | 0 | 0.025 | Prompt-cache invalidation, not guidance. compose_system (repos/_agent/programs/toolcaller@v1.py:976) ends the system prompt with _memory_cat… |
| 10.1 | PASS | 3 | 0 | 0.018 |  |
| 10.2 | PASS | 8 | 3 | 0.038 | currentUserSpace is None in this chat, yet the TypeError tells the model to use currentUserSpace, so it lost a turn and then called list_spa… |
| 10.3 | PARTIAL | 8 | 5 | 0.038 | 1) The None-currentUserSpace error points back at currentUserSpace (same as 10.2). 2) Knowledge that the previous run already searched is no… |
| 10.4 | PARTIAL | 12 | 5 | 0.072 | Assumed 'plants' means type page and, on a gap, confirmed with the same filter. It then stated not-deleted/not-trashed without any bin query… |
| 11.1 | PASS | 4 | 1 | 0.028 | Turn 2 only re-printed the skill body because turn 1 self-truncated it to skill[:3000]; model slip, not missing guidance. |
| 11.2 | PASS | 5 | 0 | 0.025 |  |
| 11.3 | PARTIAL | 8 | 4 | 0.061 | (1) The skill description ('When the user says "status", list the To do tasks in Garden.') reads like a complete instruction, so the model s… |
| 11.4 | PASS | 8 | 0 | 0.047 |  |
