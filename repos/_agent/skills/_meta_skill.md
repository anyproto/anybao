# Skill: _meta_skill

## Authoring programs

You can author PROGRAMS too (ADR-013) — real guest Python, saved in
the working space, live on the next use(). When a job needs code on
a schedule (a mail watch, a periodic check), write a program and
register an agent_triggers record for it — never a cron that wakes
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
  edit_markdown. p.update_program replaces the whole source;
  p.delete_program removes it.
- A failed post-save probe returns `{ok: false, saved: true, hint}` —
  the source IS saved; fix it with edit_program.
- **Secrets**: a program never sees a key. It passes
  `credential={"ref": "local.key.<service>", "header": ..., "prefix":
  ..., "about": {"label": ..., "hosts": ["host[:port]"], "help": ...}}`
  on each http call (help(http.get)); the first call with no stored
  value posts a credential card and the human enters the key there.
  about.hosts is mandatory — the only destination the key is ever
  sent to. `connector.key.*` names are the reviewed connectors'; a
  program of yours cannot use them for a new service.
- Overlay-exported specs (agent:/connectors:) are refused — those
  change only through the deploy pipeline.

**System skills** (deploy-pipeline-managed — `_core`, `_soul`, `_any`,
`_memory`, `_meta_skill`) carry the leading
underscore, are composed into the prompt in full, and get overwritten on
every deploy. Don't `_`-prefix your own skills. `_soul` is the identity:
its body opens the system prompt verbatim, free text, no structure the
harness reads. To change who you are, the user edits a `_soul` skill in
the working space (it shadows the shipped one; a blank body does not).
