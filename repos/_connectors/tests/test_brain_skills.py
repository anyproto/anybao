"""The brain's skills: each is found by its file name, and brain-core routes to it."""

from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "skills"


def _skill(name):
    return (SKILLS / f"{name}.md").read_text()


def test_every_brain_skill_names_itself_in_its_heading():
    for path in sorted(SKILLS.glob("brain-*.md")):
        assert path.read_text().splitlines()[0] == f"# Skill: {path.stem}", path.name


def test_brain_core_routes_investors_to_the_investor_record():
    core = _skill("brain-core")
    assert "## Investors" in core
    assert 'get_skill("brain-investor-record")' in core


def test_the_investor_record_writes_the_editors_own_markers():
    # the spellings the editor reads a toggle and a callout from
    record = _skill("brain-investor-record")
    for marker in ('<!-- any:collapsible {"open":false} -->', "<!-- any:collapsible-content -->",
                   "<!-- /any:collapsible -->", '<!-- any:callout {"icon":"💡"} -->',
                   "<!-- /any:callout -->"):
        assert marker in record, marker
