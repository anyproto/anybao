"""Offline coverage for the anyclient prep that unblocks the parallel
tracks: the SSE subscribe primitive (fake line source) and the agent-
memory CRUD wire shapes."""

import pytest
from anybao.anyclient import AnyClient, _parse_sse

# --- SSE frame parsing -------------------------------------------------------

def test_parse_sse_folds_frames_in_order():
    lines = iter([
        "event: ready", "data: {}", "",
        "event: snapshot", 'data: {"records": [1, 2], "total": 2}', "",
        ": heartbeat",
        "event: changes", 'data: {"added": [3]}', "",
        "event: closed", 'data: {"reason": "server_shutdown"}', "",
    ])
    frames = list(_parse_sse(lines))
    assert [f["event"] for f in frames] == ["ready", "snapshot", "changes", "closed"]
    assert frames[1]["data"] == {"records": [1, 2], "total": 2}
    assert frames[3]["data"]["reason"] == "server_shutdown"


def test_parse_sse_multiline_data_joined():
    frames = list(_parse_sse(iter(["event: x", "data: line1", "data: line2", ""])))
    assert frames[0]["data"] == "line1\nline2"   # non-JSON falls back to raw


def test_subscribe_uses_sse_transport_and_builds_dataset_body():
    calls = []
    def fake_sse(path, body):
        calls.append((path, body))
        yield from ["event: ready", "data: {}", "",
                    "event: closed", 'data: {"reason":"sdk_closed"}', ""]
    c = AnyClient(lambda *a: (200, {}), sse_transport=fake_sse)
    frames = list(c.subscribe_dataset("s1", "obj1", "agent_trigger_runs", limit=50))
    assert calls[0][0] == "/v1/spaces/s1/query/subscribe"
    assert calls[0][1] == {"objectId": "obj1", "dataset": "agent_trigger_runs", "limit": 50}
    assert [f["event"] for f in frames] == ["ready", "closed"]


def test_subscribe_without_transport_raises():
    c = AnyClient(lambda *a: (200, {}))
    with pytest.raises(RuntimeError, match="no sse_transport"):
        list(c.subscribe("/v1/spaces/s1/query/subscribe", {}))


# --- agent memory CRUD wire shapes -------------------------------------------

def test_memory_crud_paths_and_bodies():
    cap = []
    def fake(method, path, body):
        cap.append((method, path, body))
        if path.endswith("/agent/brain"):
            return 200, {"objectId": "brain1"}
        return 200, {"recordIds": ["mem1"]}
    c = AnyClient(fake)

    assert c.get_brain("s1")["objectId"] == "brain1"
    assert c.create_memory("s1", {"category": "lesson", "context": "x"})["recordIds"] == ["mem1"]
    c.evolve_memory("s1", "mem1", {"accessCount": 3})
    c.delete_memory("s1", "mem1")

    methods = [(m, p) for m, p, _ in cap]
    assert methods == [
        ("GET", "/v1/spaces/s1/agent/brain"),
        ("POST", "/v1/spaces/s1/agent/memory"),
        ("PATCH", "/v1/spaces/s1/agent/memory/mem1"),
        ("DELETE", "/v1/spaces/s1/agent/memory/mem1"),
    ]
    assert cap[1][2] == {"category": "lesson", "context": "x"}
    assert cap[2][2] == {"accessCount": 3}
