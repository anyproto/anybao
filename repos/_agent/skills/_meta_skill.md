# Skill: _meta_skill

## Available User Skills

User skills are `agent_skill` objects the user curates as reusable
playbooks. **Only each skill's title and one-line description are
injected below** — the body is not. When the current turn matches one
of these entries, fetch the body *before* you plan, and follow it:

```python
c = use("any@v1").client()
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

**System skills** (deploy-pipeline-managed — `_core`, `_soul`, `_any`,
`_memory`, `_space_context`, `_meta_skill`) carry the leading
underscore, are excluded from the list above, and get overwritten on
every deploy. Don't `_`-prefix your own skills.
