### verify() [getter]
Connectivity check + key validator (pulls a 1-item note page). Returns
`{ok, connected: true}` or `{ok: false, error}`. Use right after a key
is seeded.

### list_notes(created_after?, folder_id?, cursor?, limit?, max_items?) [getter]
The user's meeting notes, newest-first. `created_after` is ISO-8601;
`limit` is the page size (default 25, capped 100). Without `max_items`
returns one page + `nextCursor`; with it, follows the cursor across
pages (bounded) and returns up to `max_items` rows. Returns `{ok,
notes, nextCursor}`. A note only appears once its AI summary +
transcript have finished generating.

### get_note(id, include_transcript?) [getter]
One note with its summary; `include_transcript=true` adds the raw
transcript. Returns `{ok, note}`. A freshly-ended meeting may 404
until generation finishes — retry shortly.

### list_folders(cursor?, limit?, max_items?) [getter]
Accessible folders (hierarchy via parent_folder_id), cursor-paginated
with the same paging contract as list_notes. Returns `{ok, folders,
nextCursor}`.
