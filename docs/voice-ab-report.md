# Voice A/B report — identity-first prompt + recency anchor (2026-09-07)

Point-in-time measurement behind the ADR-005 §5 amendments of 2026-09-07
(identity first, recency anchor). The contract lives in the ADR; this
file keeps the numbers and the method so the experiment can be re-run.

## What was compared

Three prompt arms, everything else identical:

- **branch** — `feat/soul-first-anchor`: `_soul` body opens the system
  block verbatim, `_core`/`_any` state method only, conduct policy in
  `_core`, the four-message recency anchor before the user message.
- **noanchor** — the branch system block with the four anchor messages
  deleted.
- **main** — the pre-amendment prompt: `_soul` as a headed skill inside
  the band, `_core` opening "You are a code-synthesis agent", `_any`
  opening "You are an object-first agent", no anchor.

Not compared: apronkin's PR #31 (voice tag on every tool result) and a
second-call reply rewriter. The rewriter was ruled out beforehand on 100
real replies × 4 cheap models: every model either dropped offers,
questions and caveats or added sentences; the failures are semantic and
no cheap gate catches them.

## Method

1. **Live run.** 21 messages to the staging bao (gemini-3.7-flash,
   :7134 user server, :7021 repo owner, serve on 7016) covering
   greetings, emotional and snarky turns, factual reads, page creation,
   search, an opinion, a reminder, a table request, a delete request,
   a haiku, a recap and two multi-cell tasks (4 and 8 turns).
2. **Exact wire replay.** For each run the LAST model call was taken
   from the trace as sent (`http.post` record: system, messages, tools,
   cell results) and re-issued per arm. The main arm swaps the skill
   blocks by their `# Skill:` boundaries and deletes the anchor; the
   noanchor arm deletes the anchor only. Same history, same user text,
   same tool results — only the prompt differs.
3. **Two history windows.** The recorded 20-message boot window, and a
   window of 38 real old-voice exchanges taken from other rigs' traces
   (report-genre replies with headings, bold labels and em dashes) in
   place of it.
4. **Blind pairwise judging.** claude-sonnet-5 sees the current
   `_soul.md`, the user message and two replies in random order and
   grades voice (1–5), size fit (1–5), report-genre shape (bool) and a
   winner. Pairs where one arm returned no text (rate limit, credit
   error, empty completion) are excluded.
5. **Models.** gemini-3.7-flash through its own endpoint; claude-sonnet-5
   via OpenRouter; gpt-5.6-terra via the OpenAI key (`reasoning_effort:
   none`); claude-opus-5 via the Anthropic key with the body converted
   to the Messages format and `thinking: disabled` (with thinking on,
   Opus returned only a thinking block when tools were restricted).
   Two samples per arm, except Opus with one.

## Results

### Live run (branch only, gemini-3.7-flash)

21 replies: median 21 words, longest 107 (an opinion that earned it),
no em dashes by the model, no tables in chat (the table request went to
a page with a link), delete request answered by listing the 30 objects
and asking, reminder set and fired, anchor demo question ("how many
notes did you tag?") answered honestly rather than parroted. Weak spot:
the 8-turn audit came back with a `### What I found:` heading.

### Gemini 3.7 Flash, recorded 20-message window

| pairs | branch wins | other wins | tie |
|---|---|---|---|
| branch vs main (41) | 34 | 6 | 1 |
| branch vs noanchor (42) | 19 | 12 | 11 |

| arm | voice | size fit | report-genre | words | em dashes |
|---|---|---|---|---|---|
| branch | 3.95 | 4.76 | 3/41 | 33 | 2 |
| main | 2.95 | 3.24 | 15/41 | 58 | 13 |
| noanchor | 3.24 | 4.12 | 10/42 | 40 | 2 |

Where main won it had added a dry aside the branch reply lacked; the
branch is drier and shorter, main is occasionally funnier and longer.

### Old-voice window (38 exchanges), all models

| model | branch vs main W/L/T | branch vs noanchor W/L/T | report-genre branch / main / noanchor | voice branch / main | words branch / main |
|---|---|---|---|---|---|
| gemini-3.7-flash | 34 / 1 / 2 | 27 / 5 / 8 | 1 / 21 / 19 | 4.1 / 2.6 | 36 / 55 |
| claude-sonnet-5 | 26 / 5 / 0 | 22 / 7 / 2 | 1 / 16 / 15 | 4.2 / 3.0 | 66 / 97 |
| gpt-5.6-terra | 27 / 6 / 3 | 16 / 11 / 8 | 3 / 16 / 8 | 3.7 / 3.0 | 39 / 43 |
| claude-opus-5 (1 sample) | 16 / 1 / 0 | 12 / 6 / 2 | 0 / 11 / 7 | 4.2 / 3.2 | 78 / 114 |

Em dashes over the window: Gemini, GPT and Opus write none under the
branch prompt; Sonnet still writes them (39 across 35 replies, against
93 for main).

## Reading

- The change is real on every model tested: 4:1 or better against main,
  report-genre shape from roughly half of replies to near zero, 20–30%
  fewer words.
- Drift is carried by history. Main and noanchor both degrade sharply
  once the window is full of report-genre replies (Gemini: report-genre
  15/41 → 21/37 for main, 10/42 → 19/40 for noanchor). The branch does
  not (3/41 → 1/37).
- The anchor is noise on the short window and decisive on the old-voice
  window: the system-block identity is outvoted by the transcript, and
  the anchor is what holds it. Its value is smallest on gpt-5.6-terra,
  which drifts least without it.
- Main wanted a cell instead of a reply in 6 of 42 long-window Gemini
  samples and returned empty 5 times; a behavior difference, not a voice
  one, excluded from the pairs.

## Caveats

- One judge model, grading against the branch's own soul. The main
  arm's soul was the old trait list; the persona is the same, the size
  rules are not, so part of the size advantage is the content rewrite —
  which is part of the change.
- The old-voice window was ~6k tokens, well under a real 40k boot
  window. Runs were short (≤ 8 turns); drift after tens of cells is
  untested.
- n = 21 conversations; pair counts per model are 35–42, Opus 21.

## Not run

glm-5.3, kimi-k3, qwen3.8-max, deepseek-v4-pro: the OpenRouter key hit
its $20 limit during the Sonnet run. Re-running those four costs about
$25 and 20 minutes with the same scripts.

## Re-running

The scripts are small stand-alone Python (urllib only): extract the
last `http.post` per run from `anyrt trace show --seq`, build the arm
variants, sample, judge, summarize. They lived in the session scratchpad
of 2026-09-07; the method above is complete enough to rewrite them.
Keys read from `~/any/{gemini,openai,anthropic,openrouter}.key`.
