"""Offline memory-facade tests — fake transport into a real AnyClient,
fake recall + fake judge into save_with_dedup, asserting wire shapes and
each of the merge / supersede / create verdict paths (ADR-007 §2)."""

import pytest
from anybao.anyclient import AnyClient
from anybao.memory import Memory

HITS = [{"scope": "agent", "objectId": "brain1", "dataset": "agent_memory_items",
         "recordId": "old1", "score": 0.7}]


def fake_any(capture):
    def send(method, path, body):
        capture.append((method, path, body))
        return 200, {"versionId": "v1", "changeId": "c1", "recordIds": ["new1"]}
    return send


class FakeRecall:
    def __init__(self, hits=HITS, records=()):
        self.hits = hits
        self.records = {r["id"]: r for r in records}
        self.calls = []

    def search(self, query, scopes):
        self.calls.append((query, scopes))
        return self.hits

    def hydrate(self, hits):
        return [(h, self.records[h["recordId"]]) for h in hits
                if h.get("recordId") in self.records]


def verdict_judge(verdict):
    """Judge returning a fixed verdict, recording what it was shown."""
    calls = []
    def judge(candidate, hits):
        calls.append((candidate, hits))
        return verdict
    judge.calls = calls
    return judge


def memory(capture):
    return Memory(AnyClient(fake_any(capture)), "s1")


# --- add / evolve / delete ----------------------------------------------------

def test_add_wire_shape_and_item_id():
    cap = []
    got = memory(cap).add("lesson", "the fact", tags=["x"], confidence=7)
    assert got == {"itemId": "new1"}
    method, path, body = cap[0]
    assert (method, path) == ("POST", "/v1/spaces/s1/agent/memory")
    assert body == {"category": "lesson", "context": "the fact",
                    "tags": ["x"], "confidence": 7}


@pytest.mark.parametrize("category,context", [
    ("", "ctx"), ("cat", ""), ("cat", "   "), (None, "ctx"), ("cat", None)])
def test_add_raises_on_missing_required(category, context):
    with pytest.raises(ValueError, match="non-empty"):
        memory([]).add(category, context)


def test_evolve_sends_only_given_fields():
    cap = []
    assert memory(cap).evolve("m1", context="better", tags=["a"]) == {"itemId": "m1"}
    method, path, body = cap[0]
    assert (method, path) == ("PATCH", "/v1/spaces/s1/agent/memory/m1")
    assert body == {"context": "better", "tags": ["a"]}


def test_evolve_rejects_immutable_fields():
    with pytest.raises(ValueError, match="immutable/unknown.*category"):
        memory([]).evolve("m1", category="insight")   # recategorize = new item


def test_evolve_needs_a_field():
    with pytest.raises(ValueError, match="at least one field"):
        memory([]).evolve("m1")


def test_delete_wire():
    cap = []
    assert memory(cap).delete("m1") == {"itemId": "m1"}
    assert cap[0][:2] == ("DELETE", "/v1/spaces/s1/agent/memory/m1")


def test_bump_access_arithmetic():
    cap = []
    got = memory(cap).bump_access("m1", 4)
    assert got == {"itemId": "m1", "accessCount": 5}
    assert cap[0][2] == {"accessCount": 5}


# --- save_with_dedup ----------------------------------------------------------

CANDIDATE = {"category": "preference", "context": "user prefers dark mode",
             "tags": ["ui"], "confidence": 6}


def test_dedup_searches_agent_scope_and_shows_judge_the_hits():
    cap, r = [], FakeRecall()
    judge = verdict_judge({"action": "create"})
    memory(cap).save_with_dedup(CANDIDATE, recall=r, judge=judge)
    assert r.calls == [("user prefers dark mode", ["agent"])]
    assert judge.calls == [(CANDIDATE, HITS)]


def test_dedup_merge_evolves_existing_and_reports_success():
    cap = []
    got = memory(cap).save_with_dedup(
        CANDIDATE, recall=FakeRecall(),
        judge=verdict_judge({"action": "merge", "mergedInto": "old1"}))
    assert got == {"deduplicated": True, "mergedInto": "old1", "action": "merge"}
    method, path, body = cap[0]
    assert (method, path) == ("PATCH", "/v1/spaces/s1/agent/memory/old1")
    # only mutable fields flow into the evolve — category never does
    assert body == {"context": "user prefers dark mode",
                    "tags": ["ui"], "confidence": 6}


def test_dedup_supersede_creates_with_supersedes_edge():
    cap = []
    got = memory(cap).save_with_dedup(
        CANDIDATE, recall=FakeRecall(),
        judge=verdict_judge({"action": "supersede", "mergedInto": "old1"}))
    assert got == {"itemId": "new1", "action": "supersede"}
    method, path, body = cap[0]
    assert (method, path) == ("POST", "/v1/spaces/s1/agent/memory")
    assert body["edges"] == [{"to": "old1", "type": "supersedes"}]
    assert body["category"] == "preference"


def test_dedup_supersede_appends_to_existing_edges():
    cap = []
    cand = {**CANDIDATE, "edges": [{"to": "z9", "type": "related_to"}]}
    memory(cap).save_with_dedup(
        cand, recall=FakeRecall(),
        judge=verdict_judge({"action": "supersede", "mergedInto": "old1"}))
    assert cap[0][2]["edges"] == [{"to": "z9", "type": "related_to"},
                                  {"to": "old1", "type": "supersedes"}]


def test_dedup_create_is_plain_add():
    cap = []
    got = memory(cap).save_with_dedup(
        CANDIDATE, recall=FakeRecall(hits=[]), judge=verdict_judge({"action": "create"}))
    assert got == {"itemId": "new1", "action": "create"}
    assert cap[0][2] == CANDIDATE


def test_dedup_unknown_action_raises():
    with pytest.raises(ValueError, match="unknown action 'shrug'"):
        memory([]).save_with_dedup(
            CANDIDATE, recall=FakeRecall(), judge=verdict_judge({"action": "shrug"}))


@pytest.mark.parametrize("action", ["merge", "supersede"])
def test_dedup_match_actions_need_merged_into(action):
    with pytest.raises(ValueError, match="mergedInto"):
        memory([]).save_with_dedup(
            CANDIDATE, recall=FakeRecall(), judge=verdict_judge({"action": action}))


def test_dedup_candidate_validated_before_any_call():
    r = FakeRecall()
    with pytest.raises(ValueError, match="non-empty"):
        memory([]).save_with_dedup({"category": "x"}, recall=r,
                                   judge=verdict_judge({"action": "create"}))
    assert r.calls == []   # nothing hit the wire or the recall


# --- humble merge (§1b applied to the merge path; live-caught) ---------------

def test_machine_merge_never_blurs_user_stated_text_or_confidence():
    cap = []
    old = {"id": "m1", "category": "preference",
           "context": "User drinks espresso, no milk — ever.",
           "confidence": 9, "tags": ["coffee"]}
    recall = FakeRecall(hits=[{**HITS[0], "recordId": "m1"}], records=[old])
    out = memory(cap).save_with_dedup(
        {"category": "preference", "context": "Coffee drink preference",
         "confidence": 6, "source": "extraction", "tags": ["drinks"]},
        recall=recall,
        judge=verdict_judge({"action": "merge", "mergedInto": "m1"}))
    assert out["action"] == "merge"
    patches = [b for m, p, b in cap if p.endswith("/agent/memory/m1")]
    assert patches, "corroborating fields still merge"
    patch = patches[0]
    assert "context" not in patch and "body" not in patch   # text kept
    assert "confidence" not in patch                        # 9 never drops to 6
    assert patch["tags"] == ["coffee", "drinks"]            # union, not replace


def test_agent_explicit_merge_may_update_text_and_raise_confidence():
    cap = []
    old = {"id": "m1", "category": "preference",
           "context": "likes coffee", "confidence": 5,
           "edges": [{"to": "x", "type": "part_of"}]}
    recall = FakeRecall(hits=[{**HITS[0], "recordId": "m1"}], records=[old])
    memory(cap).save_with_dedup(
        {"category": "preference", "context": "espresso only, no milk",
         "confidence": 9, "edges": [{"to": "x", "type": "part_of"},
                                    {"to": "y", "type": "relates_to"}]},
        recall=recall,
        judge=verdict_judge({"action": "merge", "mergedInto": "m1"}))
    patch = [b for m, p, b in cap if p.endswith("/agent/memory/m1")][0]
    assert patch["context"] == "espresso only, no milk"     # user-stated wins in
    assert patch["confidence"] == 9                         # confidence may rise
    assert patch["edges"] == [{"to": "x", "type": "part_of"},
                              {"to": "y", "type": "relates_to"}]  # deduped union
