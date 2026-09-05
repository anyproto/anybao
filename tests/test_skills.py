"""skills/ sources — the fresh system-skill set (M4). Compose/deploy
machinery is covered by cargo (runtime/src/deploy.rs); these are the
content assertions over the markdown itself."""

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "skills"

# The fixed lead order the composer honors (runtime/src/deploy.rs).
SYSTEM_SKILL_ORDER = ("_soul", "_core", "_any", "_coding", "_memory",
                      "_space_context", "_meta_skill", "_onboarding")


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


def test_soul_is_a_verbatim_identity_that_obeys_its_own_style():
    # ADR-005 §5: first bytes of the prompt, no heading; the voice tag rides
    # a `Voice:` line (deploy sets no description); its own hard rule: no
    # em dashes — the model imitates the soul's prose
    soul = load_skills_dir(SKILLS_DIR)["_soul"]
    assert soul.startswith("You are Bao")
    assert "\u2014" not in soul
    voice = [ln for ln in soul.splitlines() if ln.startswith("Voice: ")]
    assert len(voice) == 1 and len(voice[0]) - len("Voice: ") <= 120
    for needle in ("## Voice", "## Size", "## Conduct", "## Examples",
                   "Cells stay plain code"):
        assert needle in soul, f"_soul.md lost {needle!r}"


def test_core_keeps_method_and_client_facts_not_reply_shape():
    core = load_skills_dir(SKILLS_DIR)["_core"]
    assert core.startswith("# Skill: _core\n\nYou act through one tool")
    assert "You are" not in core                       # the soul is the only identity
    for shape in ("≤300 words", "done / pending / next", "Keep final replies"):
        assert shape not in core, f"_core.md still owns reply shape: {shape!r}"
    assert "do not render" in core and "any://o/spaceId/objectId" in core


def test_onboarding_is_gated_and_silent_afterwards():
    ob = load_skills_dir(SKILLS_DIR)["_onboarding"]
    assert ob.startswith("# Skill: _onboarding\n\nApplies only while")
    assert "this skill is silent" in ob
