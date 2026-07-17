### create(space, name, source, state?, readme?) [mutator]
New mini app. `name` is the addressing key (must be free — taken name
errors, use `update`), `source` the full HTML (runtime script tags
auto-injected if missing → `warnings`), `state` any JSON-serializable
value (or a pre-serialized string), `readme` markdown. Returns
`{ok, id, name, warnings?}`.

### update(space, name, source?, state?, readme?, title?) [mutator]
Patch any subset — `None`/omitted fields stay untouched (so `update`
cannot CLEAR state; that's `set_state(space, name, None)`). `title`
renames the display name — AVOID: name is the addressing key and
lookups by the old name break. Returns `{ok, id, name, warnings?}`.

### edit(space, name, old_string, new_string, replace_all?, block?) [mutator]
Surgical replacement in `block` (`"source"` default, or `"state"`).
`old_string` must match exactly once unless `replace_all=True`
(ambiguity errors tell you the match count). Returns `{ok, id, name,
block, replacements, length_before, length_after, warnings?}`; failures
carry `{ok: False, error, length_before?}`.

### get(space, name, frm?, to?) [getter]
The whole app `{id, name, source, state, readme}` or `None`. `state`
is parsed JSON (raw string if unparseable, `None` if absent). `frm`/`to`
slice the source by 1-indexed inclusive line numbers and add a
`range: {from, to, totalLines}` descriptor.

### get_source(space, name, frm?, to?) [getter]
Just `{name, source}` (sliced with `range` when `frm`/`to` given), or
`None`. Read a big app in windows instead of whole.

### list(space) [getter]
Every mini app in the space: `[{id, name}]`, name-sorted.

### set_state(space, name, state) [mutator]
Overwrite the persisted state that `useAnytypeState` reads — the way
to reset or migrate an app's data. `None` clears. Returns
`{ok, id, name}`.

### get_state(space, name) [getter]
Parsed state object; `None` when the app is missing, state is empty,
or the stored text isn't valid JSON.

### upsert_readme(space, name, readme) [mutator]
Set the readme markdown (`""` clears). Returns `{ok, id, name}`.
