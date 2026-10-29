# Skill: brain-investor-record

Build or refresh one investor's record — a helicopter view on top, depth folded underneath, every line tied to a source — from their mail, call transcripts and the user's notes.

## When to use it

Run it for one person at a time: when the user asks for an investor's record, or asks to bring one up to date. When the conversation is about someone who is an investor and their record is missing or older than their latest mail, offer it once, in one line; run it only after a yes. It writes a page, so never run it on your own idea.

A person is an investor when any of these holds:

- they are filed under an investor collection: the catalog `investor` role (`investor_type`, `investor_status`, `check_size`, `portfolio`), or a collection the user keeps for investors (a "Current Investors" list);
- their brain facts say so (`connectors:brainRecall@v1.person(id)`: a fund, "invests personally", angel, VC, LP, partner at a fund);
- their mail is about an investment: a round, a SAFE or a note, a term sheet, a cap table, a wire, a commitment.

Someone excluded from the brain is never read for this. If the person is excluded, say so and stop.

## The record and its template

The record is the person's object in the space where the user keeps their investors, filed under that collection. Search for it before creating anything (`query_objects` by name and by `brain_person.emails`). When the collection has an investor profile template, read it before writing: the template wins on layout, this skill wins on sourcing rules.

## 1. Scope the sources

- **Mail:** every `email_messages` row whose `participants` holds one of the person's addresses. Take the addresses from their record and from `brainRecall.person(id)`, and check signatures for secondary ones. Group by `threadId`. Drop calendar invites and broadcasts where they are only cc'd, but keep their dates for the date math.
- **Also:** meeting transcripts and notes in the space (`search(space, "<their name>")`, the `meetings` scope), and the user's own notes about them.
- **Start from what the brain already holds:** `brainRecall.person(id)` gives their facts (each with its quote and source), what the user promised them, and their latest mail. Read the sources again for anything you cite.
- Report raw → kept counts before writing; the volume is signal.

## 2. Email thread objects

When the space has an email thread type (`email_thread`), keep one per real thread (`participants`, `thread_date`, `message_count`, `gmail_thread_id`), linked through the investor's `emails` relation. Search before creating one.

## 3. Properties

Facts live in properties; the page points to them. Set only what the record's collection defines:

- `email`, `phone` and `location` only as stated in mail, never guessed.
- `fund_name`, or "invests personally".
- `job_title` as "<role> at <org>".
- The `organization` relation: append, never overwrite.
- `introduced_by` from an actual intro line, else blank.
- `first_contact`, `last_email`, and `last_call` from real call evidence only.
- `total_investment` is the sum of invested checks (signed and wired).
- With the catalog `investor` role: set `investor_status` with the same words.

Words:

- **invested** = money wired;
- **committed** = promised in writing, but not yet signed and wired;
- **indicated** = an amount floated, no promise.

Never call wired money "committed".

## 4. The page, in this order

Write the body in Any's markdown, with the toggle and callout markers exactly as below. They are how a toggle and a callout are written. Never write any other `<!-- any:… -->` comment (a table's id line is the editor's own).

- **Toggle:** `<!-- any:collapsible {"open":false} -->`, a blank line, the summary, a blank line, `<!-- any:collapsible-content -->`, the numbered citations, then `<!-- /any:collapsible -->`. Each marker is on its own line, with blank lines between blocks.
- **Callout:** `<!-- any:callout {"icon":"💡"} -->` … `<!-- /any:callout -->`.
- **What stays visible:** the summary does; only the evidence folds. Last discussions opens by default (`{"open":true}`); every other toggle starts closed.
- **Toggle titles:** one paragraph. Join a multi-part summary with bold lead-ins, or the editor breaks the fold.

The sections:

1. **Header:** "Notes drafted by Bao — not written by <the user's name>."
2. **Sources**, one line:
   - N threads where they actually wrote (not just cc'd), N transcripts, and the other sources;
   - "last updated <date>".
3. **Helicopter callout**, four cited lines: where they stand · what's open · who makes the next move · total investment.
4. **Last discussions** (toggle):
   - The last several threads, transcripts and user notes → topics, conclusions, sentiment.
   - Read the latest substantive mail or transcript in full: objections, conditions, what would change their mind.
5. **Next steps:** a `- [ ]` checklist, `- [x]` once done, each item with owner, promised date and a [source].
6. **Mentions** (toggle): where they come up in other people's mail, calls and notes.
7. **Investment history** (one toggle):
   - The title is one line: the investment total and the number of checks.
   - Inside:
     - one paragraph per check (amount, instrument, vehicle, date, what it converted into);
     - a Rights line, if any;
     - a "Not committed" line for indicated amounts;
     - then all the numbered citations.
8. **Relationship history:**
   - First, a verdict callout: 4–5 short paragraphs, no new claims.
     - What it's about, and who drives it.
     - The vibe and the doubts; vision vs execution.
     - How they helped, with specifics.
     - Offers that didn't land, and whose side stalled.
     - What they want back.
   - Then a toggle: how we met, what we did together, what they asked for, what the user promised.
9. **Introductions**, two toggles, each with its citations inside:
   - "Made, N": who, role, company, when, each [n].
   - "Offered, no email record of it happening, N": what, when, each [n].
10. **Preferences and details**, five toggles:
    - Views: opinions, reading.
    - Work life: role changes, boards, theses, expertise (point to the properties).
    - Personal details.
    - Tastes.
    - Preferences: channel, the name they go by.

## 5. Citations

- **Numbering:**
  - Number every claim in a summary: [1], [2].
  - The toggle lists the same numbers: `[1] *"exact quote" — Who, Date* [source](<link>)`.
  - No number, no claim.
- **Placement:** outside toggles, the citation goes on its own line under the claim, never inline.
- **Exact substrings only:**
  - A mail quote links to its message: `any://o/<space>/<mailbox id>/email_messages/<message id>`.
  - A transcript quote links to its exact line: `any://o/<space>/<meeting id>/transcript/<segment id>`.
- **One claim, one quote.** A claim with no quote gets cut.
- **The user's own words:** don't quote the user back to themselves. Use a one-line summary and a [source], unless their wording is the fact (a promise, a number, a deadline).
- **Bao's advice:** it carries no quote and is labelled "Bao's suggestion".
- **Personal details and tastes:**
  - Only what the person said, with the date they said it; never inferred. A wrong detail costs more trust than a missing one.
  - An empty section reads "nothing in the record".
- **Self-check:** count the claims against the numbers in each section before saving.

## 6. Two-way link

Every cited thread or meeting gets a "Referenced in <person>'s notes" section: the claim and the quote, grouped by page section. Add it with `append_markdown`, or update it with `edit_markdown`.

## Rules

- State absence explicitly. Write "no email record of X", never "never did X": calls and meetings in person leave no trace.
- Ask before overwriting a whole page. Otherwise make surgical edits with `edit_markdown`, never get → replace → put.
- Search before creating a person or an organization.
- Never cite an excluded person's mail.
