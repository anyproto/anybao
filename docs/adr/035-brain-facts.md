# ADR-035: The AI brain's facts about people — from mail, quoted and grounded

Status: **Proposed** (2026-10-26)
Date: 2026-10-26
Builds on: ADR-033 (the brain in the agent overlay), ADR-034 (people,
promises and tasks), ADR-005 (the loop core and its backends), ADR-021
(credentials)
Plan: any-ui `docs/plans/2026-10-25-ai-brain-phase-2-world-model.md`
(this ADR covers its mail half; facts from meeting transcripts wait for
meeting sync)

## Context

Phase 2 of the AI brain gives it a model of the people the user works
with: where someone lives, how they want to be addressed, what they
said they care about. It is the first brain step that reads mail with
a model, so two things matter more than recall: every fact must be
traceable to the exact sentence it came from, and nothing may read
mail with a model until the user said so.

## Decision

### 1. The model reads mail only after the user's consent

A config row `brain.model` (`{allowed: true|false, requestedAt, by:
"chat"}`) is the consent. Only `brainSetup.allow_model()` and
`deny_model()` write it, after the user's plain-words yes in chat to
this question: may the brain read your mail with a model to keep facts
about the people you write with? (The `brain-core` skill carries the
wording; no client writes the row — owner decision, 2026-10-27, the
same as the brain's own switch, ADR-033 §2.) It is absent, so off, until
then, and separate from the switch. It covers every brain job that
reads mail with a model — the facts job here and the promises job
(ADR-034 §4) — each of which returns `{skipped: "consent"}` without
reading a message or calling a model unless `allowed` is `true`; the
people job needs no model and runs without it. `brainSetup.status()`
reports it as `modelAllowed`.

### 2. The `facts` store (hub version 3)

The setup run declares `facts` on the hub (shared; history kept, so a
supersession is auditable): `personId`, `kind`, `value`, `quote`,
`source`, `channel`, `observedAt`, `origin`, `confidence` and `model`
are written once; `status`, `supersedes`, `supersededBy`,
`moreSources`, `decidedAt` and `confirmedAt` change. A fact's id is
`sha256(personId | kind | normalized value)`, so re-extraction is
idempotent and a second source for the same value is appended to
`moreSources` (at most 5) rather than minting a duplicate. Kinds are a
closed table in the program; adding one is a program change. The
reconcile run upgrades a version-2 hub, as it did version 1.

### 3. `connectors:brainFacts@v1`, every six hours

1. **Consent, then exclusions.** No consent → skipped. The exclusion
   list is read fail-closed before any mail: missing or unreadable
   raises `exclusions_unreadable`, and so does a failed read of the
   persons it excludes by id.
2. **Work.** Persons with mail since the cursor, minus excluded ones,
   at most 40 per run, asked of the server in contact order (then id) a
   page at a time (an unordered capped read drops people the watermark
   then passes). The cut never falls inside a group that shares a contact
   time — everyone on one message does — since the watermark moves to the
   last one read and the next run starts after it; so a run can exceed 40
   by one such group. A (time, id) cursor would remove that excess and is
   left for later. A message with any excluded or blocklisted
   participant, or under a blocklisted label, is out for everyone in it; a person excluded by id is
   excluded by every address on their person object too. So is a message
   from before one of its participants was let back in (the exclusion
   list's `includedAt`, ADR-034 §6): the cursor here is per person, so
   lifting an exclusion would otherwise re-read the mail it kept out —
   found live 2026-10-28, when an included person's "I moved to Lisbon"
   from their excluded days became a fact.
3. **One model call per person** (`llm@v1.chat`, tier
   `brain.extractor`, no tools): the person's name, their current facts,
   and up to 8 000 characters of their newest mail, each message tagged
   `[M1]`, `[M2]`…. The reply is a JSON array of `{kind, value, quote,
   ref, confidence}`; one retry on an unparsable reply.
4. **Grounding, in plain code.** An item is kept only when its kind is
   in the table, its `ref` names a tagged message, its `quote` is a
   verbatim substring of that message after whitespace normalization
   (≤ 240 characters), its value is 1–200 characters, and its confidence
   (capped at 0.8) is at least 0.6. A model that paraphrases loses the
   fact: precision over recall. Every drop is counted by reason in
   `config/facts.stats`, which holds counts only, never names.
5. **Writes.** A single-valued kind (address form, language, preferred
   name, channel, location, role, company) supersedes a current
   inferred fact: the new record names `supersedes`, the old one turns
   `superseded` with `supersededBy`. A fact the user stated (or
   confirmed) is never superseded by inference; the conflict is a
   `fact_conflict` signal instead. `history.consent` lands `proposed`
   and needs the user's accept. Every fact records the model that
   proposed it.
6. **Cursor.** Per mailbox, advanced only after the person's writes
   landed; a provider error counts the person, holds the watermark and
   moves on; two in a row stop the run, and so does a run in which no
   call succeeded (`provider_failed`) — one person with new mail and a
   provider that is down would otherwise pass for done, run after run,
   with the watermark held for good and the brain `ready`.

`unpinned` is raised when no `llm.tier.brain.extractor` row exists. The
setup run seeds that row from `llm.tier.codegen` when absent, so on a
desktop whose Bao runs on the user's Codex or Claude Code login through
the shell's local AI proxy (the `anyai` backend), the extractor uses
that login with no API key.

### 4. The card is recall's person view

`brainRecall@v1.person(id)` (ADR-034 §7) is the card, one deterministic
tool and one shape: the person's columns plus `addressForm` and
`language` from their preference facts, their current facts grouped
(tastes, views, preferences, personal, work, history) each with its
verbatim quote and source, proposed facts apart, superseded ones gone,
what the user promised them with status, what they asked the user for
(`asked`, the `history.asked_for` facts), their latest mail, and
`markdown` — the card for chat, which quotes every fact and lists the
open promises and the latest threads. A separate brief tool would be a
second tool for the same question, so there is none. The view honours
the exclusion list.

## Consequences

- No runtime change: the extractor is a guest program on the existing
  `llm@v1`, and the model it uses is whatever the extractor tier names.
- Verified on a scratch `any` server with `anyrt serve`, the extractor
  tier pointing at a loopback stand-in for the desktop's local AI proxy
  that answers each request with one `codex exec` run.
- Facts from meeting transcripts, the per-meeting trigger, and the
  user's own facts wait for meeting sync and later phases.
