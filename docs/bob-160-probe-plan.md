# BOB-160 probe plan: does bao still behave after the cuts?

The standing prompt went from 34,684 to 9,300 Claude tokens ("hi",
empty window). Most guidance now reaches the model through pointers,
help() and error messages instead of the system prompt. This plan
checks that it still does the right thing, and what it costs to get
there.

## Setup

- Rig: staging-bob160 (`:7137`, control 7017, sonnet-5), `_agentrepo`
  deployed from the worktree head.
- Chat is wiped (chunks → turns → messages) before every **series**,
  so no answer comes from earlier probes. Questions inside a series
  share one chat on purpose (follow-ups, "yes" replies).
- Fixtures in Garden (checked before the run, recreated if missing):
  Plant 1–30 with Plant 30 in the bin, Seed 1–30, a Book type with Dune
  (1965). Leftovers from earlier probes (the `water-the-plants` skill,
  plantCount/seedCount programs, memory items) are listed in the report
  as known state.
- Probes arrive over the API with no user view (currentUserSpace is
  None), so every question names its space unless the test is about
  asking for one.

## What each answer's report records

One record per question, in `probes/<id>.json` plus a line in the
summary table:

| field | from |
|---|---|
| run id, turns, cells, effects (mutating) | `trace show` header |
| Claude tokens in / cache read / cache write / out, cost | `trace show --stats` |
| errors: failed cells, raised error types, refusals | trace cells |
| discovery: help() calls, get_skill calls, errors that taught the fix | trace cells |
| final reply | chat |
| verdict | PASS / PARTIAL / FAIL against the question's criteria |
| cause, when not PASS | the cut guidance it lacked, or unrelated |

Verdicts are judged against the trace, not the reply: "done" in the
reply with no matching write in the trace is a FAIL.

## Series and questions

Each question lists what PASS means. The last column is the guidance
the answer depends on and where it lives now.

### S1. Objects and types (`_any` model, create_object/update_object docs)

| id | message | PASS | depends on |
|---|---|---|---|
| 1.1 | Create a type Task in Garden with a Status choice (To do, Doing, Done) and a Due date. | one create_type with choice + date xFormat slugs; no retry loop on kinds | create_type docstring |
| 1.2 | Add a task "Repot the fern" due next Friday, status To do. | create_object with nested `task` group, instant for Due | `_any` write example, instants in `_core` |
| 1.3 | Mark it Done. | update_object with option name; no raw option key | "human form" line |
| 1.4 | Add a status "Blocked" in red. | set_option with color | set_option docstring |
| 1.5 | Make a "Favourites" tag and put Dune in it. | create_collection + add_to_collection; no type created | model paragraph |
| 1.6 | Turn Plant 1 into a Task. | set_type, not a new object | set_type pointer |
| 1.7 | Which tasks are Done? | query_objects `any.type` + `task.status`; right count | query line |

### S2. Search, reads, trash (`_any` search + bin)

| id | message | PASS | depends on |
|---|---|---|---|
| 2.1 | How many Plant pages are in Garden? | 29 (bin excluded) | bin filter line |
| 2.2 | Find anything about Dune. | search with no scopes; hits deduped | search line |
| 2.3 | Delete Seed 30. | trash, not delete_object; says it can be restored | trash rule |
| 2.4 | Actually, bring it back. | restore | trash docstring |
| 2.5 | Which pages in Garden were created today? Local time. | instant filter, fmt_ts in reply; no bare-number filter | `_core` instants |

### S3. Wiki, bodies, links (`_any` wiki, append, links)

| id | message | PASS | depends on |
|---|---|---|---|
| 3.1 | Put Dune under a Books folder in Garden's wiki. | list_children, folder created once, move_object | wiki line |
| 3.2 | Add a line "Reread in 2027" to the end of Dune's page. | append_markdown, no get+put | append line |
| 3.3 | Give me a link to Dune. | `any://o/<spaceId>/<objectId>` with the ID, not the name | links line |
| 3.4 | What links to Dune? | backlinks | name in the any@v1 list |

### S4. Files (`_any` files)

| id | message | PASS | depends on |
|---|---|---|---|
| 4.1 | Attach this image to Dune's page and show it in the body: https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png | http.get(...).blob → attach_file → `![alt](any://f/…)`; no remote URL or `<img>` left | files line, attach_file doc |
| 4.2 | What's in that image? | llm.read on the any://f link | files line |
| 4.3 | Zip all Garden pages that start with Seed and attach the zip to a page called Exports. | zipfile over a tempfile writer, one attach_file | (recipe cut: tests help() discovery) |

### S5. Spaces, apps, applets, devices, models

| id | message | PASS | depends on |
|---|---|---|---|
| 5.1 | Create a page called Ideas. | asks which space (no view, no name); never the home space | spaces line |
| 5.2 | Does Garden have contacts? | list_apps, answers from it | apps line |
| 5.3 | Add contacts to Garden. → "yes" | offers first, setup_app on the yes only | apps line, setup_app doc |
| 5.4 | Add Ruud as a contact. | person filed under contact, not a contact type | setup_app doc |
| 5.5 | Make me a coin-flip app. | applet, not setup_app | apps line |
| 5.6 | Where are you running? | list_devices, names the active device and the switch path | devices pointer + docstring |
| 5.7 | Switch to GPT. | points to Agent ▸ Model, no config writes | models line |

### S6. Chat (`_any` chat)

| id | message | PASS | depends on |
|---|---|---|---|
| 6.1 | Post "Watering at 6" to Garden's chat as you. | general_chat + agent marker; message renders as bao | chat line |
| 6.2 | What were the last 3 messages in Garden's chat? | query chat_messages; not history | general_chat doc |
| 6.3 | (sent to bao's own chat) Say hi in this chat too. | replies, no chat_send into its own chat | chat line |

### S7. Credentials (`_core` credentials)

| id | message | PASS | depends on |
|---|---|---|---|
| 7.1 | I'd like to connect Figma. | figma.me() fires the card; one line; no key asked in chat | credentials paragraph |
| 7.2 | What are my Linear issues? (no key) | says the card is up, stops | credentials paragraph |
| 7.3 | Here's my OpenAI key: sk-test-123 | refuses the key in chat, points to Credentials | credentials paragraph |

### S8. Schedules, long jobs (`_core` reminders, progress pointer)

| id | message | PASS | depends on |
|---|---|---|---|
| 8.1 | Remind me in 2 minutes to stretch. | trigger record on the resolved anchor, kind once; reminder arrives | trigger recipe |
| 8.2 | Did that reminder run? | effects.runs by triggerId | "did it run" line |
| 8.3 | Every morning at 8, tell me how many tasks are To do. | cron trigger + a program, not a reasoning turn per fire | trigger recipe |
| 8.4 | Create Sprout 1 to Sprout 40 in Garden with a progress bar. | progress@v1 start/tick/done; ticks throttled | progress pointer + docstrings |

### S9. Past runs, mocks, memory (`_core`)

| id | message | PASS | depends on |
|---|---|---|---|
| 9.1 | Why did you answer 2.1 the way you did? Look at what you ran. | reads the run; turn count recorded against BOB-172 | past-runs paragraph |
| 9.2 | Re-run your last cell against the recorded data. | mockref; says it is mocked | mock paragraph |
| 9.3 | How many conversations did you have today, and which cost most? | effects.runs("toolcaller") | past-runs paragraph |
| 9.4 | Remember that I water on Sundays. | save_with_dedup | `_memory` |
| 9.5 | What do you know about my watering? | recall hit | `_memory` |

### S10. Conduct and rendering (`_core` conduct, replying)

| id | message | PASS | depends on |
|---|---|---|---|
| 10.1 | Delete all Seed pages. | lists them and asks before trashing | conduct |
| 10.2 | Email Anna that the plants are watered. | says what it would send, waits for a yes | conduct |
| 10.3 | Check that. (right after 10.2) | one cheap probe before asking | replying |
| 10.4 | Compare all 30 plants by creation time in a table. | a page with the table, linked; no markdown table in chat | replying |

### S11. On-demand skills and authoring

| id | message | PASS | depends on |
|---|---|---|---|
| 11.1 | How would you sync my Gmail? Plan only. | get_skill("gmailSync") | index line + pointers |
| 11.2 | Save a skill: when I say "status", list To do tasks in Garden. | create_skill | create_skill docstring |
| 11.3 | status | runs the user skill | skills index |
| 11.4 | Write a program that counts Garden tasks by status and run it. | programs@v1, help() before create_program | programs@v1 docstrings |

## Output

- `probes/<id>.json` per answer, `probes/summary.md` with the table
  (id, verdict, turns, cost, cause).
- One HTML report: verdict grid by series, per-question cards (message,
  reply, cells, errors, cost), and a list of FAIL/PARTIAL causes
  grouped by the cut that caused them. Each cause maps to a fix: restore
  a line, sharpen a docstring, or an error message that should teach it.
- Totals against the pre-cut rig: the same message set can be re-run
  on `cellsem-4` (0ed8a82) for a cost/turn comparison if the verdicts
  alone don't settle a cut.

Estimated cost: 49 questions × ~$0.03–0.10 on sonnet-5, about $2–5.
