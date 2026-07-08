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
    for needle in ("run_cell", "values.get", "use(", 'use("any@v1")',
                   "save_with_dedup", 'use("llm@v1")', "chat_send", "any://",
                   "batch"):
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
        if path.endswith("/types") and method == "GET":
            return 200, {"types": [{"id": "t-skill", "xKey": "agent_skill"}]}
        if path.endswith("/types"):
            return 200, {"typeId": "t-skill"}
        if path.endswith("/properties") and method == "GET":
            return 200, {"properties": []}
        if path.endswith("/properties"):
            return 200, {"propId": "p-name"}
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


def test_tool_docs_and_category_sections():
    from anybao.skills import memory_categories_section, tool_docs_section

    def send(method, path, body):
        if path.endswith("/objects/query"):
            return 200, {"records": [{"id": "p1", "program": {"name": "websearch"}}]}
        if path.endswith("/agent/brain"):
            return 200, {"objectId": "brain1"}
        if path.endswith("/query"):
            ds = body["dataset"]
            if ds == "program_description":
                return 200, {"records": [{"id": "main", "text": "Searches the web."}]}
            if ds == "program_methods":
                return 200, {"records": [
                    {"id": "go", "name": "go(q)", "kind": "getter", "pos": 0}]}
            return 200, {"records": [{"id": "m1", "category": "preference"},
                                     {"id": "m2", "category": "lesson"},
                                     {"id": "m3", "category": "lesson"}]}
        return 404, {"error": {"code": "unknown", "message": path}}

    client = AnyClient(send)
    docs = tool_docs_section(client, "s1")
    assert "### websearch" in docs and "Searches the web." in docs
    assert "- `go(q)` [getter]" in docs
    cats = memory_categories_section(client, "s1")
    assert cats == "Memory categories in use: lesson, preference"


def test_deploy_dir_survives_existing_type_and_reports_statuses():
    fake = FakeAny(existing=None)
    statuses = SkillDeployer(AnyClient(fake), space="s1").deploy_dir(SKILLS_DIR)
    assert set(SYSTEM_SKILL_ORDER) <= set(statuses)
    assert all(s == "created" for s in statuses.values())
