"""Tool-markdown splitter — THE one splitter (plan §4: kill the dual
Go/JS splitters). Parses a tool-description file into a description body
+ per-method docs, matching the canonical format the deploy tool writes
to `program_description` / `program_methods`:

    ## Tool Description
    <body>

    ## Tool Schema        (or `# Tools` / `## Tools`)
    ### method(sig) [kind]
    <method body>
    ### other(sig) [kind]
    ...

`kind` ∈ getter|mutator|setup|program (default getter). Bare method
name (record id) is the text before `(`. Duplicate bare names get a
`-<pos>` suffix. Mirrors internal/agentlog... err, cmd/bobrik-watch/
toolmd.go — kept in ONE language now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_KIND_TAG = re.compile(r"\s*\[(getter|mutator|setup|program)\]\s*$")
_TOOLS_HEADINGS = ("# Tools", "## Tools", "## Tool Schema")


@dataclass
class MethodDoc:
    bare_name: str   # record id (name before "(")
    name: str        # full heading (signature)
    kind: str        # getter | mutator | setup | program
    text: str        # method body
    pos: int         # order


def _extract_section(md: str, section: str) -> str:
    captured: list[str] = []
    capturing = False
    level = 0
    for line in md.split("\n"):
        m = _HEADING.match(line)
        if m:
            lvl = len(m.group(1))
            title = m.group(2).strip()
            if capturing and lvl <= level:
                break
            if title.lower() == section.lower():
                capturing = True
                level = lvl
                continue
        if capturing:
            captured.append(line)
    return "\n".join(captured).strip()


def _bare_name(name: str) -> str:
    i = name.find("(")
    return name[:i].strip() if i > 0 else name.strip()


def split_tool_markdown(md: str) -> tuple[str, list[MethodDoc]]:
    """→ (description, methods)."""
    description = _extract_section(md, "Tool Description")

    # find the tools/schema section start
    start = -1
    section_level = 1
    for line_no, line in enumerate(md.split("\n")):
        stripped = line.strip()
        m = _HEADING.match(line)
        if m and any(stripped == h for h in _TOOLS_HEADINGS):
            start = line_no
            section_level = len(m.group(1))
            break
    if start == -1:
        return description, []

    lines = md.split("\n")
    method_prefix = "#" * (section_level + 1) + " "
    methods: list[MethodDoc] = []
    seen: set[str] = set()
    cur: MethodDoc | None = None
    cur_lines: list[str] = []

    def flush() -> None:
        nonlocal cur, cur_lines
        if cur is None:
            return
        cur.text = "\n".join(cur_lines).strip("\n")
        if cur.bare_name in seen:
            cur.bare_name = f"{cur.bare_name}-{cur.pos}"
        seen.add(cur.bare_name)
        methods.append(cur)
        cur, cur_lines = None, []

    for line in lines[start + 1:]:
        m = _HEADING.match(line)
        if m and len(m.group(1)) <= section_level:
            break  # next sibling/parent ends the schema walk
        if line.startswith(method_prefix):
            flush()
            heading = line[len(method_prefix):].strip()
            kind = "getter"
            km = _KIND_TAG.search(heading)
            if km:
                kind = km.group(1)
                heading = heading[: km.start()].strip()
            cur = MethodDoc(bare_name=_bare_name(heading), name=heading,
                            kind=kind, text="", pos=len(methods))
            cur_lines = []
        elif cur is not None:
            cur_lines.append(line)
    flush()
    return description, methods
