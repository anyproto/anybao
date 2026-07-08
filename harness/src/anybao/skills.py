"""Skills — the system-prompt components (M4 "skills written fresh").

System skills (`_`-prefixed markdown in `skills/`) deploy as
`agent_skill` objects with editor/markdown content and compose into the
loop's system prompt in a fixed order. User skills are non-underscore
`agent_skill` objects the user curates; only their title/description
lines enter the prompt (the `_meta_skill` skill documents the
fetch-on-demand contract).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from .anyclient import AnyClient, AnyError

SKILL_TYPE = "agent_skill"
SYSTEM_SKILL_ORDER = ("_soul", "_core", "_any", "_memory",
                      "_space_context", "_meta_skill")


def load_skills_dir(path: str | Path) -> dict[str, str]:
    """{name: markdown} from `<name>.md` files."""
    return {p.stem: p.read_text() for p in sorted(Path(path).glob("*.md"))}


def compose_system(skills: dict[str, str], *,
                   user_skill_lines: list[str] | None = None) -> str:
    """Fixed-order composition; unknown system skills append sorted
    after the known ones; the user-skill inventory (titles only) lands
    right after _meta_skill, which explains it."""
    ordered = [n for n in SYSTEM_SKILL_ORDER if n in skills]
    ordered += sorted(n for n in skills if n not in ordered)
    parts = []
    for name in ordered:
        parts.append(skills[name].strip())
        if name == "_meta_skill" and user_skill_lines:
            parts.append("\n".join(f"- {line}" for line in user_skill_lines))
    return "\n\n".join(parts)


class SkillDeployer:
    """Skills counterpart of Deployer: agent_skill objects, markdown
    content, hash-gated by content comparison."""

    def __init__(self, client: AnyClient, *, space: str):
        self._c = client
        self._space = space

    def _ensure_type(self) -> None:
        # idempotent: an existing type errors, which is the fine case
        with contextlib.suppress(AnyError):
            self._c.create_type(self._space, {"name": "Agent Skill",
                                              "xKey": SKILL_TYPE})

    def _find(self, name: str) -> str | None:
        recs = self._c.query_objects(
            self._space, filter={f"{SKILL_TYPE}.name": name}, limit=1)
        return recs[0]["id"] if recs else None

    def deploy_one(self, name: str, content: str) -> str:
        oid = self._find(name)
        if oid is not None:
            if self._c.get_markdown(self._space, oid) == content:
                return "unchanged"
            self._c.put_markdown(self._space, oid, content)
            return "updated"
        res = self._c.create_object(self._space, {
            "types": [SKILL_TYPE],
            "initialProperties": {"any": {"name": name},
                                  SKILL_TYPE: {"name": name}}})
        self._c.put_markdown(self._space, res["objectId"], content)
        return "created"

    def deploy_dir(self, src_dir: str | Path) -> dict[str, str]:
        self._ensure_type()
        return {name: self.deploy_one(name, content)
                for name, content in load_skills_dir(src_dir).items()}


def tool_docs_section(client: AnyClient, space: str) -> str:
    """The stable block's tool-docs slice (ADR-005 §5): every deployed
    any_tool program's description + method inventory."""
    progs = client.query_objects(space, filter={"program.any_tool": True})
    parts = []
    for p in progs:
        name = (p.get("program") or {}).get("name", "?")
        desc = client.query(space, p["id"], "program_description")
        methods = client.query(space, p["id"], "program_methods")
        block = [f"### {name}"]
        if desc:
            block.append(desc[0].get("text", ""))
        block += [f"- `{m.get('name')}` [{m.get('kind', 'getter')}]"
                  for m in sorted(methods, key=lambda m: m.get("pos", 0))]
        parts.append("\n".join(block))
    return "## Tools\n\n" + "\n\n".join(parts) if parts else ""


def memory_categories_section(client: AnyClient, space: str) -> str:
    """Category-name inventory (ADR-007 §5 — cheap, cache-stable; the
    write path's vocabulary anchor)."""
    brain = client.get_brain(space)["objectId"]
    items = client.query(space, brain, "agent_memory_items", limit=500)
    cats = sorted({i.get("category") for i in items if i.get("category")})
    return "Memory categories in use: " + ", ".join(cats) if cats else ""


def load_skills_space(client: AnyClient, space: str) -> dict[str, str]:
    """System skills back from a space — the runtime side of compose."""
    recs = client.query_objects(space, filter={f"{SKILL_TYPE}.name":
                                               {"$regex": "^_"}})
    out = {}
    for r in recs:
        name = (r.get(SKILL_TYPE) or {}).get("name") or (r.get("any") or {}).get("name")
        if name:
            out[name] = client.get_markdown(space, r["id"])
    return out
