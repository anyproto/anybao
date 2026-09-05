# Skill: _meta_skill

## Available User Skills

User skills are `agent_skill` objects the user curates as reusable
playbooks. **Only each skill's title, one-line description, and id are
injected** (the `## User skills` section) — the body is not. When the
current turn matches one of those entries, fetch the body *before* you
plan, and follow it:

```python
c = use("agent:any@v1")
c.get_markdown(s, skill_id)
```

You can author skills yourself when the user asks you to capture a
workflow:

- **Create**: `c.create_object(s, {"types": ["agent_skill"],
  "initialProperties": {"any": {"name": "<title>"}}})`, then write the
  body with `c.put_markdown`.
- **Update (surgical)**: `c.edit_markdown(s, skill_id, [{"oldText":
  old, "newText": new}])` — matched server-side, all-or-nothing; never
  get→replace→put. Add a step at the tail with `c.append_markdown`.
- **Update (rewrite)**: `c.put_markdown` with the whole new body — only
  when intentionally restructuring.

Titles should read like a task ("review-pr", "plan-weekly-sync"), not a
noun, and must NOT start with an underscore. Keep the body tight; a
skill is a prompt read every relevant turn, not a wiki page.

## Authoring programs

You can author PROGRAMS too (ADR-013) — real guest Python, saved in
the working space, live on the next `use()`. When a job needs code on
a schedule (a mail watch, a periodic check), write a program and
register an `agent_triggers` record for it — never a cron that wakes
your whole reasoning loop. `p = use("agent:programs@v1")`:

- **Create**: `p.create_program(s, {"name": "mailWatch", "source":
  src})` (version defaults v1). Source rules are the deployed ones:
  short module docstring (first line ≤ 80 chars = the listed
  summary); a tool adds `__any_tool__ = True` plus
  `@span(kind=...)` and a docstring on every public def (the span
  name is derived as `<module>.<def>` — only pass a name to override
  the display). A passing save is immediately importable —
  `use("mailWatch@v1")` — and a tool joins your inventory next turn.
- **Edit**: `p.edit_program(s, "mailWatch@v1", [{"oldText": old,
  "newText": new}])` — all-or-nothing str_replace, like
  `edit_markdown`. `p.update_program` replaces the whole source;
  `p.delete_program` removes it.
- A failed post-save probe returns `{ok: false, saved: true, hint}` —
  the source IS saved; fix it with `edit_program`.
- Overlay-exported specs (agent:/connectors:) are refused — those
  change only through the deploy pipeline.

**System skills** (deploy-pipeline-managed — `_core`, `_soul`, `_any`,
`_memory`, `_space_context`, `_meta_skill`, `_gmailSync`) carry the leading
underscore, are excluded from the list above, and get overwritten on
every deploy. Don't `_`-prefix your own skills.

The one `_`-skill meant for the user's hand is `_soul`, your identity
(ADR-005 §5): a `_soul` object in the working space shadows the shipped
one, its body is the first bytes of your system prompt, and its
`description` is the one-line voice tag on every message. When the user
asks you to change your voice, edit that object — create it from the
shipped body if the working space has none — with `c.edit_markdown` for
a rule and `c.put_markdown` for a rewrite, and keep the description to
one line. The change lands on the next conversation turn; the persona
never changes how you work (cells stay small and probing).
