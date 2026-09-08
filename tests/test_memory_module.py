"""programs/memory@v1 — the memory write facade as a guest module,
tested host-side by exec-ing the guest source with fake `effect`/
`span`/`use` globals. A fake any@v1 client captures the write calls, a
fake recall@v1 object answers search/hydrate, and a fake llm chat
returns the judge verdict as text — asserting the merge / supersede /
create paths, validation, and the humble merge (ADR-007 §2)."""

import json
from pathlib import Path

import pytest

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
SRC = (PROGRAMS_DIR / "memory@v1" / "program.py").read_text()


def load(use=None):
    g = {
        "effect": lambda name, payload: pytest.fail(f"unexpected effect {name!r}"),
        "span": lambda name=None, kind=None: (lambda f: f),
        "use": use or (lambda spec: pytest.fail(f"unexpected use({spec!r})")),
    }
    exec(compile(SRC, "memory@v1.py", "exec"), g)
    return g


MEM = load()

HITS = [{"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
         "recordId": "old1", "score": 0.7}]

OLD1 = {"id": "old1", "category": "preference", "context": "some dark ui fact"}


class FakeClient:
    """Captures any@v1 memory write calls like the wire."""

    def __init__(self):
        self.calls = []

    # the verbs take no space: memory has one home (ADR-017 §0); the
    # capture keeps a fixed "s1" slot so the assertions read as before
    def create_memory(self, fields):
        self.calls.append(("create", "s1", fields))
        return {"versionId": "v1", "changeId": "c1", "recordIds": ["new1"]}

    def evolve_memory(self, item_id, fields):
        self.calls.append(("evolve", "s1", item_id, fields))
        return {}

    def delete_memory(self, item_id):
        self.calls.append(("delete", "s1", item_id))
        return {}


class FakeRecall:
    def __init__(self, hits=HITS, records=(OLD1,)):
        self.hits = hits
        self.records = {r["id"]: r for r in records}
        self.calls = []

    def search(self, query, scopes):
        self.calls.append((query, scopes))
        return self.hits

    def hydrate(self, hits):
        return [(h, self.records[h["recordId"]]) for h in hits
                if h.get("recordId") in self.records]


def verdict_chat(verdict):
    """llm chat returning the judge verdict wrapped in prose (so the
    `_first_json` extraction is exercised), recording what it was asked."""
    calls = []

    def chat(messages, system="", tier="codegen", tools=None):
        calls.append({"messages": messages, "system": system,
                      "tier": tier, "tools": tools})
        return {"parts": [{"type": "text",
                           "text": f"Verdict: {json.dumps(verdict)}"}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    chat.calls = calls
    return chat


def memory(client, chat=None):
    return MEM["memory"](client, llm_chat=chat or verdict_chat({"action": "create"}))


# --- add / evolve / delete ----------------------------------------------------

def test_add_call_shape_and_item_id():
    c = FakeClient()
    got = memory(c).add("lesson", "the fact", tags=["x"], confidence=7)
    assert got == {"itemId": "new1"}
    assert c.calls == [("create", "s1", {"category": "lesson", "context": "the fact",
                                         "tags": ["x"], "confidence": 7})]


@pytest.mark.parametrize("category,context", [
    ("", "ctx"), ("cat", ""), ("cat", "   "), (None, "ctx"), ("cat", None)])
def test_add_raises_on_missing_required(category, context):
    with pytest.raises(ValueError, match="non-empty"):
        memory(FakeClient()).add(category, context)


def test_evolve_sends_only_given_fields():
    c = FakeClient()
    assert memory(c).evolve("m1", context="better", tags=["a"]) == {"itemId": "m1"}
    assert c.calls == [("evolve", "s1", "m1", {"context": "better", "tags": ["a"]})]


def test_evolve_rejects_immutable_fields():
    with pytest.raises(ValueError, match="immutable/unknown.*category"):
        memory(FakeClient()).evolve("m1", category="insight")   # recategorize = new item


def test_evolve_needs_a_field():
    with pytest.raises(ValueError, match="at least one field"):
        memory(FakeClient()).evolve("m1")


def test_delete_call():
    c = FakeClient()
    assert memory(c).delete("m1") == {"itemId": "m1"}
    assert c.calls == [("delete", "s1", "m1")]


def test_bump_access_arithmetic():
    c = FakeClient()
    got = memory(c).bump_access("m1", 4)
    assert got == {"itemId": "m1", "accessCount": 5}
    assert c.calls == [("evolve", "s1", "m1", {"accessCount": 5})]


def test_default_llm_chat_comes_from_llm_module():
    seen = []

    class FakeLlmModule:
        @staticmethod
        def chat(messages, system="", tier="codegen", tools=None):
            seen.append(tier)
            return {"parts": [{"type": "text", "text": '{"action": "create"}'}],
                    "stop": "done", "usage": {}}

    used = []
    g = load(use=lambda spec: used.append(spec) or FakeLlmModule())
    mem = g["memory"](FakeClient())
    mem.save_with_dedup(CANDIDATE, FakeRecall())
    assert used == ["llm@v1"] and seen == ["classify"]


# --- save_with_dedup ----------------------------------------------------------

CANDIDATE = {"category": "preference", "context": "user prefers dark mode",
             "tags": ["ui"], "confidence": 6}


def test_dedup_searches_agent_scope_and_shows_judge_the_hits():
    r = FakeRecall()
    chat = verdict_chat({"action": "create"})
    memory(FakeClient(), chat).save_with_dedup(CANDIDATE, r)
    assert r.calls == [("user prefers dark mode", ["agent"])]
    call = chat.calls[0]
    assert call["tier"] == "classify" and call["tools"] == []
    assert "deduplicate" in call["system"]
    prompt = call["messages"][0]["parts"][0]["text"]
    assert "CANDIDATE: [preference] user prefers dark mode" in prompt
    assert "id=old1" in prompt   # the hydrated hit was shown to the judge


def test_dedup_merge_evolves_existing_and_reports_success():
    c = FakeClient()
    got = memory(c, verdict_chat({"action": "merge", "mergedInto": "old1"})) \
        .save_with_dedup(CANDIDATE, FakeRecall())
    assert got == {"deduplicated": True, "mergedInto": "old1", "action": "merge"}
    kind, space, item_id, body = c.calls[0]
    assert (kind, space, item_id) == ("evolve", "s1", "old1")
    # only mutable fields flow into the evolve — category never does
    assert body == {"context": "user prefers dark mode",
                    "tags": ["ui"], "confidence": 6}


def test_dedup_supersede_creates_with_supersedes_edge():
    c = FakeClient()
    got = memory(c, verdict_chat({"action": "supersede", "mergedInto": "old1"})) \
        .save_with_dedup(CANDIDATE, FakeRecall())
    assert got == {"itemId": "new1", "action": "supersede"}
    kind, space, body = c.calls[0]
    assert (kind, space) == ("create", "s1")
    assert body["edges"] == [{"to": "old1", "type": "supersedes"}]
    assert body["category"] == "preference"


def test_dedup_supersede_appends_to_existing_edges():
    c = FakeClient()
    cand = {**CANDIDATE, "edges": [{"to": "z9", "type": "related_to"}]}
    memory(c, verdict_chat({"action": "supersede", "mergedInto": "old1"})) \
        .save_with_dedup(cand, FakeRecall())
    assert c.calls[0][2]["edges"] == [{"to": "z9", "type": "related_to"},
                                      {"to": "old1", "type": "supersedes"}]


def test_dedup_no_hydrated_items_creates_without_model_call():
    c = FakeClient()
    chat = verdict_chat({"action": "merge", "mergedInto": "old1"})   # must not be asked
    got = memory(c, chat).save_with_dedup(CANDIDATE, FakeRecall(records=()))
    assert got == {"itemId": "new1", "action": "create"}
    assert chat.calls == []
    assert c.calls[0] == ("create", "s1", CANDIDATE)


def test_dedup_unknown_action_raises():
    with pytest.raises(ValueError, match="unknown action"):
        memory(FakeClient(), verdict_chat({"action": "shrug"})) \
            .save_with_dedup(CANDIDATE, FakeRecall())


def test_dedup_judge_reply_without_json_raises():
    def chat(messages, system="", tier="codegen", tools=None):
        return {"parts": [{"type": "text", "text": "merge them I guess"}],
                "stop": "done", "usage": {}}
    with pytest.raises(ValueError, match="no JSON object"):
        memory(FakeClient(), chat).save_with_dedup(CANDIDATE, FakeRecall())


@pytest.mark.parametrize("action", ["merge", "supersede"])
def test_dedup_match_actions_need_merged_into(action):
    with pytest.raises(ValueError, match="mergedInto"):
        memory(FakeClient(), verdict_chat({"action": action})) \
            .save_with_dedup(CANDIDATE, FakeRecall())


def test_dedup_candidate_validated_before_any_call():
    c, r = FakeClient(), FakeRecall()
    with pytest.raises(ValueError, match="non-empty"):
        memory(c).save_with_dedup({"category": "x"}, r)
    assert r.calls == [] and c.calls == []   # nothing hit the wire or the recall


# --- humble merge (§2 machine humility on the merge path; live-caught) -------

def test_machine_merge_never_blurs_user_stated_text_or_confidence():
    c = FakeClient()
    old = {"id": "m1", "category": "preference",
           "context": "User drinks espresso, no milk — ever.",
           "confidence": 9, "tags": ["coffee"]}
    recall = FakeRecall(hits=[{**HITS[0], "recordId": "m1"}], records=[old])
    out = memory(c, verdict_chat({"action": "merge", "mergedInto": "m1"})) \
        .save_with_dedup(
            {"category": "preference", "context": "Coffee drink preference",
             "confidence": 6, "source": "extraction", "tags": ["drinks"]},
            recall)
    assert out["action"] == "merge"
    patches = [body for kind, _, item, body in c.calls
               if kind == "evolve" and item == "m1"]
    assert patches, "corroborating fields still merge"
    patch = patches[0]
    assert "context" not in patch and "body" not in patch   # text kept
    assert "confidence" not in patch                        # 9 never drops to 6
    assert patch["tags"] == ["coffee", "drinks"]            # union, not replace


def test_agent_explicit_merge_may_update_text_and_raise_confidence():
    c = FakeClient()
    old = {"id": "m1", "category": "preference",
           "context": "likes coffee", "confidence": 5,
           "edges": [{"to": "x", "type": "part_of"}]}
    recall = FakeRecall(hits=[{**HITS[0], "recordId": "m1"}], records=[old])
    memory(c, verdict_chat({"action": "merge", "mergedInto": "m1"})) \
        .save_with_dedup(
            {"category": "preference", "context": "espresso only, no milk",
             "confidence": 9, "edges": [{"to": "x", "type": "part_of"},
                                        {"to": "y", "type": "relates_to"}]},
            recall)
    patch = [body for kind, _, item, body in c.calls
             if kind == "evolve" and item == "m1"][0]
    assert patch["context"] == "espresso only, no milk"     # user-stated wins in
    assert patch["confidence"] == 9                         # confidence may rise
    assert patch["edges"] == [{"to": "x", "type": "part_of"},
                              {"to": "y", "type": "relates_to"}]  # deduped union
