# Skill: _space_context

The system prompt embeds a singleton **Main Space Context** object — a
bird's-eye README of this space describing *structure and conventions*.
Every new conversation reads it. When Main grows past a soft ceiling it
splits into child context files; their titles + ids appear in the
**Child Context Files** section and you fetch their content on demand.

Main is **NOT a database.** Per-object state changes often and belongs
in the objects themselves, looked up at the moment of need. Main also
goes stale — verify non-meta facts (types, objects, counts) at lookup
rather than trusting Main.

### What belongs

- Types and their key properties
- Naming, tag, and platform conventions
- Active tools and programs (names only, no source)
- Domain workflows and pipelines
- Standing rules and decisions ("prefer X over Y")
- Pointers to child context files

### What does NOT belong

- Per-object IDs, ratings, statuses, progress — query them live
- Full property lists — `c.list_properties(space, type_id)` at need
- Ephemeral state, drafts — those ride chat-history compression

### Entry style: rules, not inventory

A good entry stays true after the user adds five more objects of that
kind tomorrow. If it would need updating just because the space grew,
it's inventory — don't write it.

- Avoid: "~10 movies tracked; Neuromancer is the only finished book"
- Prefer: "On create, check for name duplicates"

### How to edit

Main's id is in the `[Main](any://spaceId/objectId)` link at the top of
its section. Surgical edit from a cell (`c = use("any@v1").client()`):

```python
md = c.get_markdown(s, main_id)
assert md.count(old) == 1
c.put_markdown(s, main_id, md.replace(old, new))
```

After editing, don't re-output Main — the next turn's prompt reflects
it. When a rule changes, keep both forms at a high level:
`RULE: bookmarks use category tags (was: emoji prefix until 2026-04)`.

### Don't

- **Delete** space-context files — only the post-turn split may merge
  or discard them.
- **Duplicate** facts across Main and a child — link, or lift into Main.
- **Summarize** — space context is source of truth, not a summary.
- **Enumerate objects** — Main is a nav map, not a catalogue.
