"""Skill bodies by name — what the bound get_skill() global serves.

An on-demand skill (a non-`_` agent_skill, ADR-009 §3) rides the prompt
as one `## Skills` index line; its body is fetched when a turn needs it.
The toolcaller binds `get_skill = use("agent:skills@v1").binder(spaces)`
in the run's context cell, `spaces` in lookup order: the bao (working)
space, then the agent overlay, then the connectors overlay.
"""

# Not an __any_tool__: the model reaches it only through get_skill().


def _skill_type(c, space):
    return next((t["id"] for t in c.list_types(space)
                 if (t.get("xKey") or t.get("key")) == "agent_skill"), None)


def _find(c, space, name):
    """The skill's body in one space, or None when it has no such skill
    (or only a blank one — a blank body never shadows, ADR-005 §5)."""
    if not _skill_type(c, space):
        return None
    for o in c.query_objects(space, filter={"any.type": "agent_skill"}):
        if ((o.get("any") or {}).get("name") or "") == name:
            body = c.get_markdown(space, o["id"]) or ""
            return body if body.strip() else None
    return None


def _names(c, spaces):
    seen = set()
    for space in spaces:
        if not _skill_type(c, space):
            continue
        for o in c.query_objects(space, filter={"any.type": "agent_skill"}):
            n = (o.get("any") or {}).get("name") or ""
            if n and not n.startswith("_"):
                seen.add(n)
    return sorted(seen)


def binder(spaces):
    """Bind get_skill over `spaces` (space ids, lookup order; None and
    repeats dropped) → the get_skill function."""
    order = []
    for s in spaces:
        if s and s not in order:
            order.append(s)

    @span(name="skills.get_skill", kind="getter")  # noqa: F821 - guest global
    def get_skill(name):
        """A skill's markdown body by name — read it, then follow it.

        Looks in the bao space first (a skill of your own shadows a shipped
        one), then the agent repo, then the connectors repo. An unknown
        name raises LookupError listing the skills that exist."""
        c = use("agent:any@v1")  # noqa: F821 - guest global
        for space in order:
            body = _find(c, space, name)
            if body is not None:
                return body
        raise LookupError(f"no skill named {name!r}; known: {', '.join(_names(c, order))}")

    return get_skill
