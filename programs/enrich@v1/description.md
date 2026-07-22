Enrichment: turns a markdown transcript (a call/meeting note living as
an object's editor body) into a structured, SOURCED enrichment of a
space — not markdown edits. Facts land as `enriched_data` records on
target objects (for a property enrichment the real property is set AND
the source recorded); every fact keeps provenance back to its
transcript blocks (`any://space/transcript#blockId,…`).

Three stages, two of them here:

```python
en = use("enrich@v1")
p = en.propose(space, transcript_id)        # stage 1: draft proposal
# → {ok, proposalId, proposalLink, items, tally}  (nothing applied)
# stage 2 — YOURS: read the enrich_proposal_items records, consolidate
# by GROUPING (same newName+newType for facets of one topic; set
# targetObjectId to route to existing objects), drop redundancies.
# Edit items in place; never rewrite text/source (that kills
# provenance). The user reviews the proposal OBJECT, not your message.
r = en.apply(space, p["proposalId"])        # stage 3: after approval
# → {ok, created, propertiesSet, enrichedDataWritten, proposalDeleted,
#    failures}  — deterministic, deletes the proposal
```

Slow (2 LLM passes over the whole transcript) and writes a draft
object — reach for it when the user asks to enrich/ingest a
transcript into a space. Never apply before the user approves the
proposal. Failures return `{ok: False, error}` — never raises.
