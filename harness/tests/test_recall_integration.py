"""Recall integration tests against a real any server (`-m integration`).

Search rides the server's ASYNC indexer, so the search test writes, then
polls up to ~10s before asserting — and skips (not fails) if the index
never populates: embedder/indexer timing is environmental, the wire
contract is what's pinned here. by_period and neighbors read directly
(no index) and assert firmly.
"""

import time
import uuid

import pytest
from anybao.anyclient import AnyError

pytestmark = pytest.mark.integration


def _poll_search(recall, query, *, scopes, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = recall.search(query, scopes=scopes)
        if hits:
            return hits
        time.sleep(0.5)
    return []


def test_search_finds_written_content(client, fresh_space, guest_use):
    # any.name always indexes under scope `basic` (prop chunker) — the
    # agent-data datasets are excluded from indexing today, so an object
    # name is the reliable searchable write.
    token = f"zanzibar{uuid.uuid4().hex[:8]}"
    client.create_object(fresh_space, {"initialProperties": {"any": {"name": f"note {token}"}}})

    r = guest_use("recall@v1").recall(guest_use("any@v1").client(), fresh_space)
    try:
        hits = _poll_search(r, token, scopes=("agent", "history", "basic"))
    except Exception as e:
        if getattr(e, "code", "") == "index.disabled":
            pytest.skip("search index disabled on this server")
        raise
    if not hits:
        pytest.skip("index not populated — embedder/indexer timing")
    hit = hits[0]
    assert hit["scope"] == "basic"
    assert {"objectId", "dataset", "recordId", "score"} <= hit.keys()


def test_by_period_fans_out_across_real_datasets(client, fresh_space, guest_use):
    now = int(time.time())
    chat = client.create_object(fresh_space, {"types": ["chat"]})["objectId"]
    client.append_turn(fresh_space, chat,
                       {"userText": "hello", "replies": ["hi"], "llm": {"stopReason": "done"}})
    client.create_chunk(fresh_space, chat, {
        "summary": "hour one", "level": 1, "fromSeq": 0, "toSeq": 0,
        "periodStart": now - 60, "periodEnd": now})
    brain = client._call("GET", f"/v1/spaces/{fresh_space}/agent/brain")["objectId"]
    client._call("POST", f"/v1/spaces/{fresh_space}/agent/memory",
                 {"category": "lesson", "context": "test fact"})

    r = guest_use("recall@v1").recall(guest_use("any@v1").client(), fresh_space,
                                      brain_object_id=brain, chat_object_id=chat)
    recs = r.by_period(now - 3600, now + 3600)
    assert {x["source"] for x in recs} == {"memory", "turn", "chunk"}
    # merged list is time-ascending on each source's natural field
    ts = [x.get("validFrom") or x.get("periodStart") or x.get("createdAt") or 0 for x in recs]
    assert ts == sorted(ts)
    # out-of-range window is empty
    assert r.by_period(now - 7200, now - 7100) == []


def test_neighbors_forward_refs_live(client, fresh_space, guest_use):
    # Object refs are links-format properties: arrays of any://<id> URIs
    # (an object-KIND property would reject a bare id string). Helper
    # doesn't take a format kwarg, so define the property via the client.
    tid = client.create_type(fresh_space, {"name": "Note", "xKey": "note"})["typeId"]
    pid = client.add_property(fresh_space, tid, {
        "name": "Relates To", "xKey": "relates_to", "format": {"type": "links"}})["propId"]
    target = client.create_object(fresh_space, {
        "types": [tid], "initialProperties": {"any": {"name": "target"}}})["objectId"]
    source = client.create_object(fresh_space, {
        "types": [tid],
        "initialProperties": {"any": {"name": "source"},
                              tid: {pid: [f"any://{target}"]}}})["objectId"]

    rec = guest_use("recall@v1").recall(guest_use("any@v1").client(), fresh_space)
    got = rec.neighbors(source)
    assert [f["targetId"] for f in got["forward"]] == [target]  # any:// stripped
    assert got["forward"][0]["propName"] == "Relates To"

    # the reverse read: target's backlinks include source through the prop
    try:
        client.backlinks(fresh_space, target)
    except AnyError as e:
        if e.code == "request.not_found":
            pytest.skip("server predates the /backlinks route")
        raise
    back = rec.neighbors(target)
    assert {"sourceId": source, "typeId": tid, "propId": pid} in back["backlinks"]
