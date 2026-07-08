"""skills/ sources + skills.py — the fresh system-skill set (M4) and
its compose/deploy machinery."""

from pathlib import Path

from anybao.anyclient import AnyClient
from anybao.skills import (
    SYSTEM_SKILL_ORDER,
    SkillDeployer,
    compose_system,
    load_skills_dir,
)

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def test_skill_sources_cover_the_system_set():
    skills = load_skills_dir(SKILLS_DIR)
    assert set(SYSTEM_SKILL_ORDER) <= set(skills)


def test_core_skill_documents_the_real_surface():
    core = load_skills_dir(SKILLS_DIR)["_core"]
    for needle in ("run_cell", "values.get", "effects.of", "use(",
                   "any.query", "memory.save_with_dedup", "llm.chat",
                   "chat.send", "any://"):
        assert needle in core, f"_core.md lost {needle!r}"
    # no legacy-JS residue (no-backcompat: fresh shapes)
    for stale in ("console.log", "anyHelper.", "convmemory", "var result"):
        assert stale not in core, f"_core.md still speaks JS: {stale!r}"


def test_memory_skill_carries_the_save_table_and_vocabulary():
    mem = load_skills_dir(SKILLS_DIR)["_memory"]
    assert "✓ Save" in mem and "✗ Skip" in mem          # the ported table
    assert "1–2 per turn" in mem                          # the budget
    assert "deduplicated" in mem                          # merge-is-success
    for edge in ("relates_to", "caused_by", "supersedes", "decided_in",
                 "part_of", "owned_by", "discussed_in"):
        assert edge in mem


def test_compose_orders_and_injects_user_skill_lines():
    skills = {"_core": "CORE", "_soul": "SOUL", "_meta_skill": "META",
              "_zeta": "ZETA"}
    out = compose_system(skills, user_skill_lines=["review-pr — walk the diff"])
    assert out.index("SOUL") < out.index("CORE") < out.index("META")
    assert out.index("META") < out.index("- review-pr") < out.index("ZETA")


class FakeAny:
    def __init__(self, existing=None, content=""):
        self.calls = []
        self.existing = existing   # object id or None
        self.content = content

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path.endswith("/types"):
            return 409, {"error": {"code": "type.exists", "message": "dup"}}
        if path.endswith("/objects/query"):
            recs = [{"id": self.existing}] if self.existing else []
            return 200, {"records": recs}
        if path.endswith("/editor/markdown") and method == "GET":
            return 200, {"content": self.content}
        if path.endswith("/editor/markdown"):
            return 200, {}
        if path.endswith("/objects"):
            return 200, {"objectId": "new1"}
        return 404, {"error": {"code": "unknown", "message": path}}


def test_deploy_one_create_update_unchanged():
    fresh = FakeAny(existing=None)
    assert SkillDeployer(AnyClient(fresh), space="s1").deploy_one("_core", "X") \
        == "created"
    # content landed via put_markdown on the new object
    assert any(p.endswith("new1/editor/markdown") for _, p, _ in fresh.calls)

    same = FakeAny(existing="o1", content="X")
    assert SkillDeployer(AnyClient(same), space="s1").deploy_one("_core", "X") \
        == "unchanged"
    assert not any(m == "PUT" for m, p, _ in same.calls)

    changed = FakeAny(existing="o1", content="OLD")
    assert SkillDeployer(AnyClient(changed), space="s1").deploy_one("_core", "X") \
        == "updated"


def test_deploy_dir_survives_existing_type_and_reports_statuses():
    fake = FakeAny(existing=None)
    statuses = SkillDeployer(AnyClient(fake), space="s1").deploy_dir(SKILLS_DIR)
    assert set(SYSTEM_SKILL_ORDER) <= set(statuses)
    assert all(s == "created" for s in statuses.values())
