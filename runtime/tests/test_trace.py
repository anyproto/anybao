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
    assert idx.pop("e", k)["output"] == "first"
    assert idx.pop("e", k)["output"] == "second"
    assert idx.pop("e", k) is None
