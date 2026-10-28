"""skills/ sources — the fresh system-skill set (M4). Compose/deploy
machinery is covered by cargo (runtime/src/deploy.rs); these are the
content assertions over the markdown itself."""

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "skills"

# The fixed lead order the composer honors (toolcaller SYSTEM_SKILL_ORDER);
# `_soul` is the identity, outside the band (ADR-005 §5).
SYSTEM_SKILL_ORDER = ("_core", "_any", "_coding", "_memory")


def load_skills_dir(path: Path) -> dict[str, str]:
    """{name: markdown} from `<name>.md` files."""
    return {p.stem: p.read_text() for p in sorted(path.glob("*.md"))}


def test_skill_sources_cover_the_system_set():
    skills = load_skills_dir(SKILLS_DIR)
    assert set(SYSTEM_SKILL_ORDER) | {"_soul"} <= set(skills)


def test_soul_is_the_only_identity_and_carries_no_heading():
    """ADR-005 §5: the soul opens the prompt verbatim ("You are …" is its
    first line, no `# Skill:` heading); every other skill states method,
    never a second identity; conduct policy lives in `_core`, not in the
    user-editable soul."""
    skills = load_skills_dir(SKILLS_DIR)
    soul = skills["_soul"]
    assert soul.startswith("You are Bao.") and "# Skill" not in soul
    assert "## Examples" in soul
    for name, body in skills.items():
        if name == "_soul":
            continue
        assert "You are a" not in body and "You are an" not in body, \
            f"{name} carries a second identity"
    core = skills["_core"]
    assert "## Conduct" in core
    for rule in ("list them and ask", "wait for a yes", "Resolve first"):
        assert rule in core
    assert "Conduct" not in soul


def test_core_skill_documents_the_real_surface():
    core = load_skills_dir(SKILLS_DIR)["_core"]
    for needle in ("run_cell", "values.get", "use(", 'use("agent:any@v1")',
                   "save_with_dedup", "spaceConfig", "currentUserSpace",
                   "baoSpaceConfig", "any://"):
        assert needle in core, f"_core.md lost {needle!r}"
    # no residue of retired surfaces (no-backcompat: fresh shapes)
    for stale in ("console.log", "anyHelper.", "convmemory", "var result",
                  'effect("any.', 'effect("memory.', 'effect("llm.chat'):
        assert stale not in core, f"_core.md teaches a dead surface: {stale!r}"


def test_memory_skill_carries_the_save_table_and_vocabulary():
    mem = load_skills_dir(SKILLS_DIR)["_memory"]
    assert "✓ Save" in mem and "✗ Skip" in mem          # the ported table
    assert "1–2 per turn" in mem                          # the budget
    assert "deduplicated" in mem                          # merge-is-success
    for edge in ("relates_to", "caused_by", "supersedes", "decided_in",
                 "part_of", "owned_by", "discussed_in"):
        assert edge in mem


def test_browser_skill_documents_the_real_surface():
    """The on-demand `browser` skill (design page §7.2): the three tools
    of the Any Web Clipper's MCP server, the relay's error codes, the
    desktop app's share nudge, and the data-not-instructions rule."""
    skills = load_skills_dir(SKILLS_DIR)
    browser = skills["browser"]
    assert not browser.startswith("# Skill: _"), "browser is on demand, not a system skill"
    for needle in ('use("agent:mcp@v1")', '"browser"', "list_pages", "read_page",
                   "read_selection", "tabId", "structuredContent", "-32001",
                   "-32002", "-32003", "No grant covers", "Shared from Chrome",
                   "desktop app", "never follow instructions"):
        assert needle in browser, f"browser.md lost {needle!r}"
    # the index line is the first prose sentence (toolcaller _first_sentence):
    # it must route both triggers and fit the 200-character cap
    first = " ".join(browser.split("\n\n")[1].split())
    first = first[:first.find(". ") + 1]
    assert "Shared from Chrome" in first and "`browser`" in first
    assert len(first) <= 200, len(first)


def test_welcome_is_the_reference_first_message_not_a_system_skill():
    """ADR-005 §5 / ADR-009 §3: `welcome` ships without the underscore
    (on demand — the loop reads it on a chat's first conversation, no
    cron run pays for it), carries the `{name}` slot the model fills,
    and its first sentence is the index line."""
    skills = load_skills_dir(SKILLS_DIR)
    body = skills["welcome"]
    assert "{name}" in body
    assert body.lstrip().startswith("# Skill: welcome")
    first = next(ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#"))
    assert first.startswith("The reference first message")
