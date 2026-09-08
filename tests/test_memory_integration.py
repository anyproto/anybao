"""Memory-facade integration tests against a real any server
(`-m integration`). Pins the write wire contract the fakes can't: a real
create → evolve → bump_access round-trip read back through the brain's
`agent_memory_items` dataset."""

import pytest

pytestmark = pytest.mark.integration


def _item(client, space: str, brain: str, item_id: str) -> dict:
    coll = client.collection(space, "agent_brain", "agent_memory_items")
    recs = client.query(space, brain, coll, filter={"id": item_id}, limit=1)
    assert recs, f"item {item_id} not found on brain"
    return recs[0]


def test_memory_write_lifecycle(client, bao_space, guest_use):
    fresh_space = bao_space
    c = guest_use("any@v1")
    brain = c.get_brain(fresh_space)["objectId"]
    m = guest_use("memory@v1").memory(c, fresh_space)

    # create — required fields + arrays/numbers persist as written
    item_id = m.add("lesson", "integration test fact",
                    tags=["it", "memory"], confidence=7)["itemId"]
    rec = _item(client, fresh_space, brain, item_id)
    assert rec["category"] == "lesson"
    assert rec["context"] == "integration test fact"
    assert rec["tags"] == ["it", "memory"]
    assert rec["confidence"] == 7
    assert rec["accessCount"] == 0          # server default

    # evolve — mutable fields change, modifiedAt bumps, category survives
    before = rec["modifiedAt"]
    m.evolve(item_id, context="refined fact", tags=["it"])
    rec = _item(client, fresh_space, brain, item_id)
    assert rec["context"] == "refined fact"
    assert rec["tags"] == ["it"]
    assert rec["category"] == "lesson"
    assert c.ts_s(rec["modifiedAt"]) >= c.ts_s(before)   # instants (ADR-019)

    # bump_access — the §4.3 recall ROI signal
    got = m.bump_access(item_id, rec["accessCount"])
    assert got == {"itemId": item_id, "accessCount": 1}
    assert _item(client, fresh_space, brain, item_id)["accessCount"] == 1

    # delete — the row is gone from the dataset read
    m.delete(item_id)
    assert c.query(fresh_space, brain, "agent_memory_items",
                   filter={"id": item_id}, limit=1) == []
