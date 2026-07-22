### propose(space, transcript_id, opts?) [mutator]
Stage 1: read the transcript object's `editor_blocks` (block-cited),
LLM-synthesize knowledge units, ground each against the space
(scope-basic search), LLM-reconcile to `new|enrich|conflict|redundant`,
then persist a DRAFT `enrich_proposal` object — one
`enrich_proposal_items` record per kept item (`redundant` dropped,
`conflict` demoted to `enrich` for review). Item shape: `{text, source,
outcome, targetObjectId, targetKind: "collection"|"property",
targetProperty: "<typeXKey>.<propXKey>", value, newType, newName}`.
`opts`: `{"limit": grounding candidates per unit (default 6)}`. Returns
`{ok: True, proposalId, proposalLink, space, transcriptId, items,
errors, tally}`; nothing is written to target objects. Errors return
`{ok: False, error}`.

### apply(space, proposal_id) [mutator]
Stage 3: deterministic server-side apply (POST /enrich/apply, shared
with the any-ui Apply button — no LLM). Creates ONE object per grouped
`newType`+`newName`, sets real properties for `property` items, writes
an `enriched_data` provenance record onto every target, then DELETES
the proposal. Returns `{ok: True, proposalId, created, propertiesSet,
enrichedDataWritten, proposalDeleted, failures}` — non-empty `failures`
still means the rest applied. Re-apply of a deleted/unknown proposal →
`{ok: False}` (404 enrich.empty_proposal).

### analyze(space, opts) [getter]
The stage-1 analysis core WITHOUT persistence — for previews and
tests. `opts`: `{"transcriptId": …}` (block-cited sources) or
`{"transcript": "<raw text>"}` (no citations), plus `limit?`. Returns
the raw report `{ok, space, transcriptId, units, grounded, actions,
tally}`.
