"""skills/ sources — the fresh system-skill set (M4). Compose/deploy
machinery is covered by cargo (runtime/src/deploy.rs); these are the
content assertions over the markdown itself."""

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "skills"

# The fixed lead order the composer honors (runtime/src/deploy.rs).
SYSTEM_SKILL_ORDER = ("_soul", "_core", "_any", "_coding", "_memory",
                      "_space_context", "_meta_skill")


def load_skills_dir(path: Path) -> dict[str, str]:
    """{name: markdown} from `<name>.md` files."""
    return {p.stem: p.read_text() for p in sorted(path.glob("*.md"))}


def test_skill_sources_cover_the_system_set():
    skills = load_skills_dir(SKILLS_DIR)
    assert set(SYSTEM_SKILL_ORDER) <= set(skills)


def test_core_skill_documents_the_real_surface():
    core = load_skills_dir(SKILLS_DIR)["_core"]
    for needle in ("run_cell", "values.get", "use(", 'use("agent:any@v1")',
                   "save_with_dedup", "spaceConfig", "currentUserSpace",
                   "baoSpaceConfig", "any://", "batch"):
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
