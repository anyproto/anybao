from pathlib import Path

import pytest
from anyrt import trace as tr


def make_trace(tmp_path: Path) -> list[dict]:
    w = tr.TraceWriter(run={"id": "t1", "program": "test@v1"})
    k1 = tr.input_key("http.get", {"url": "https://a"})
    w.effect(effect="http.get", cell="c1", input={"url": "https://a"}, key=k1,
             output={"status": 200}, meta={"class": "read"})
    w.cell(cell="c1", ok=True, metrics={"fuel_used": 123})
    p = tmp_path / "t.jsonl"
    w.dump(p)
    return tr.load(p)


def test_roundtrip_and_header(tmp_path):
    records = make_trace(tmp_path)
    assert records[0]["kind"] == "header"
    assert records[0]["schema"] == tr.SCHEMA
    assert [r["kind"] for r in records[1:]] == ["effect", "cell"]
    assert records[1]["seq"] == 1 and records[2]["seq"] == 2


def test_canonical_key_is_order_insensitive():
    a = tr.input_key("e", {"x": 1, "y": [1, 2]})
    b = tr.input_key("e", {"y": [1, 2], "x": 1})
    assert a == b
    assert a != tr.input_key("e2", {"x": 1, "y": [1, 2]})


def test_strict_cursor_matches_and_diverges(tmp_path):
    records = make_trace(tmp_path)
    cur = tr.ReplayCursor(records)
    k1 = tr.input_key("http.get", {"url": "https://a"})
    rec = cur.expect_effect("http.get", k1)
    assert rec["output"] == {"status": 200}
    cur.expect_cell("c1", ok=True)
    assert cur.exhausted()

    cur2 = tr.ReplayCursor(records)
    with pytest.raises(tr.DivergenceError):
        cur2.expect_effect("http.get", tr.input_key("http.get", {"url": "https://OTHER"}))


def test_cursor_detects_extra_and_reordered(tmp_path):
    records = make_trace(tmp_path)
    cur = tr.ReplayCursor(records)
    # reordered: cell checkpoint before the effect
    with pytest.raises(tr.DivergenceError):
        cur.expect_cell("c1", ok=True)


def test_mock_index_fifo(tmp_path):
    w = tr.TraceWriter(run={"id": "t2"})
    k = tr.input_key("e", {"n": 1})
    w.effect(effect="e", cell=None, input={"n": 1}, key=k, output="first")
    w.effect(effect="e", cell=None, input={"n": 1}, key=k, output="second")
    idx = tr.MockIndex(w.records)
    first, second = idx.pop("e", k), idx.pop("e", k)
    assert first is not None and first["output"] == "first"
    assert second is not None and second["output"] == "second"
    assert idx.pop("e", k) is None


def test_blob_spill_roundtrip(tmp_path):
    w = tr.TraceWriter(run={"id": "b1"}, blob_threshold=64)
    big = {"body": "x" * 1000}
    k = tr.input_key("http.get", {"url": "https://a"})
    w.effect(effect="http.get", cell=None, input={"url": "https://a"}, key=k, output=big)
    rec = w.records[1]
    assert set(rec["output"]) == {"__blob", "bytes"}  # spilled
    assert rec["input"] == {"url": "https://a"}       # small input inline

    p = tmp_path / "t.jsonl"
    w.dump(p)
    blobs = tr.load_blobs(p)
    assert tr.resolve_blobs(tr.load(p)[1]["output"], blobs) == big


def test_replay_resolves_spilled_output():
    from anyrt.effects import Broker, Registry, effect

    reg = Registry()

    @effect("big", kind="read", registry=reg)
    def big_effect(ctx):
        return {"body": "y" * 1000}

    w1 = tr.TraceWriter(run={"id": "b2"}, blob_threshold=64)
    Broker(reg, w1).call("big", {})
    b2 = Broker(reg, tr.TraceWriter(run={"id": "b2r"}), mode="replay",
                cursor=tr.ReplayCursor(w1.records), blobs=w1.blobs)
    assert b2.call("big", {}) == {"body": "y" * 1000}


def test_views_trace_diff_and_call_trace():
    w = tr.TraceWriter(run={"id": "v1"})
    k = tr.input_key("e", {})
    w.effect(effect="e", cell="c1", input={}, key=k, output=1, meta={"mocked": True})
    w.effect(effect="e", cell="c1", input={}, key=k, output=2, meta={"mocked": False})
    w.cell(cell="c1", ok=True)
    w.effect(effect="e", cell="c2", input={}, key=k, output=3, meta={"mocked": False})
    diff = tr.trace_diff(w.records)
    assert [r["output"] for r in diff] == [2, 3]
    ct = tr.call_trace(w.records, "c1")
    assert [r["kind"] for r in ct] == ["effect", "effect", "cell"]
