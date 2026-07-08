import pytest
from anyrt import trace as tr
from anyrt.effects import Broker, EffectError, Registry, effect


def make_registry():
    reg = Registry()
    calls = []

    @effect("http.get", kind="read", registry=reg, redact=("headers.authorization",))
    def http_get(ctx, url, headers=None):
        calls.append(url)
        if url.endswith("/boom"):
            raise ValueError("upstream 500")
        return {"status": 200, "body": f"body of {url}"}

    return reg, calls


def test_record_and_redaction():
    reg, calls = make_registry()
    w = tr.TraceWriter(run={"id": "r1"})
    b = Broker(reg, w)
    out = b.call("http.get", {"url": "https://a", "headers": {"authorization": "Bearer s3cr3t"}})
    assert out["status"] == 200
    rec = w.records[1]
    assert rec["input"]["headers"]["authorization"] == "<redacted>"
    assert "s3cr3t" not in tr.canonical_json(w.records)
    assert rec["meta"]["class"] == "read" and rec["meta"]["mocked"] is False


def test_error_is_data_and_exception():
    reg, _ = make_registry()
    w = tr.TraceWriter(run={"id": "r2"})
    b = Broker(reg, w)
    with pytest.raises(EffectError) as ei:
        b.call("http.get", {"url": "https://a/boom"})
    assert ei.value.type == "ValueError"
    assert w.records[1]["error"]["message"] == "upstream 500"


def test_replay_returns_recorded_without_executing(tmp_path):
    reg, calls = make_registry()
    w1 = tr.TraceWriter(run={"id": "r3"})
    b1 = Broker(reg, w1)
    b1.call("http.get", {"url": "https://a"})
    assert calls == ["https://a"]

    cur = tr.ReplayCursor(w1.records)
    w2 = tr.TraceWriter(run={"id": "r3-replay"})
    b2 = Broker(reg, w2, mode="replay", cursor=cur)
    out = b2.call("http.get", {"url": "https://a"})
    assert out["status"] == 200
    assert calls == ["https://a"]  # NOT executed again
    assert w2.records[1]["meta"]["mocked"] is True


def test_replay_divergence_on_changed_input():
    reg, _ = make_registry()
    w1 = tr.TraceWriter(run={"id": "r4"})
    Broker(reg, w1).call("http.get", {"url": "https://a"})
    b2 = Broker(reg, tr.TraceWriter(run={"id": "x"}), mode="replay",
                cursor=tr.ReplayCursor(w1.records))
    with pytest.raises(tr.DivergenceError):
        b2.call("http.get", {"url": "https://CHANGED"})


def test_mock_mode_unmatched_fail_vs_live():
    reg, calls = make_registry()
    w1 = tr.TraceWriter(run={"id": "r5"})
    Broker(reg, w1).call("http.get", {"url": "https://a"})

    b_fail = Broker(reg, tr.TraceWriter(run={"x": 1}), mode="mock",
                    mock_index=tr.MockIndex(w1.records), mock_unmatched="fail")
    assert b_fail.call("http.get", {"url": "https://a"})["status"] == 200
    with pytest.raises(EffectError):
        b_fail.call("http.get", {"url": "https://new"})

    b_live = Broker(reg, tr.TraceWriter(run={"x": 2}), mode="mock",
                    mock_index=tr.MockIndex(w1.records), mock_unmatched="live")
    n_before = len(calls)
    assert b_live.call("http.get", {"url": "https://new"})["status"] == 200
    assert len(calls) == n_before + 1  # executed live -> traceDiff material


# ---- spans (ADR-001 §4c broker side) ------------------------------------------

def make_span_registry():
    reg = Registry()

    @effect("http.get", kind="read", registry=reg)
    def http_get(ctx, url):
        return {"status": 200}

    @effect("db.put", kind="mutate", registry=reg)
    def db_put(ctx, value):
        return {"ok": True}

    return reg


def test_span_groups_stamps_and_counts():
    reg = make_span_registry()
    w = tr.TraceWriter(run={"id": "sp1"})
    b = Broker(reg, w)
    sid = b.span_begin("helper.sync", {"kwargs": {"n": 2}})
    b.call("http.get", {"url": "https://a"})
    b.call("db.put", {"value": 1})
    b.span_end(ok=True, output={"synced": 2})
    b.call("http.get", {"url": "https://after"})

    begin, e1, e2, end, after = w.records[1:6]
    assert sid == "s1"
    assert begin["kind"] == "span" and begin["phase"] == "begin"
    assert begin["name"] == "helper.sync" and begin["parent"] is None
    assert e1["span"] == "s1" and e2["span"] == "s1"
    assert end["ok"] is True and end["output"] == {"synced": 2}
    assert end["meta"]["effects"] == 2 and end["meta"]["mutations"] == 1
    assert "span" not in after                 # stamp only inside spans


def test_span_nesting_parent_and_counters():
    reg = make_span_registry()
    w = tr.TraceWriter(run={"id": "sp2"})
    b = Broker(reg, w)
    b.span_begin("outer", {})
    b.span_begin("inner", {})
    b.call("db.put", {"value": 1})             # counts in BOTH open spans
    b.span_end(ok=True)
    b.call("http.get", {"url": "https://a"})   # outer only, stamped innermost=outer
    b.span_end(ok=True)

    inner_begin = w.records[2]
    assert inner_begin["parent"] == "s1"
    put = w.records[3]
    assert put["span"] == "s2"                 # innermost open span
    inner_end = w.records[4]
    assert inner_end["meta"]["effects"] == 1 and inner_end["meta"]["mutations"] == 1
    get = w.records[5]
    assert get["span"] == "s1"
    outer_end = w.records[6]
    assert outer_end["meta"]["effects"] == 2 and outer_end["meta"]["mutations"] == 1


def test_span_replay_checkpoints_and_divergence():
    reg = make_span_registry()
    w1 = tr.TraceWriter(run={"id": "sp3"})
    b1 = Broker(reg, w1)
    b1.span_begin("helper.sync", {"kwargs": {"n": 1}})
    b1.call("http.get", {"url": "https://a"})
    b1.span_end(ok=True, output={"n": 1})

    # identical rerun replays clean, span records consumed as checkpoints
    b2 = Broker(reg, tr.TraceWriter(run={"id": "sp3r"}), mode="replay",
                cursor=tr.ReplayCursor(w1.records))
    b2.span_begin("helper.sync", {"kwargs": {"n": 1}})
    b2.call("http.get", {"url": "https://a"})
    b2.span_end(ok=True, output={"n": 1})

    # changed facade input diverges at the begin checkpoint
    b3 = Broker(reg, tr.TraceWriter(run={"id": "sp3d"}), mode="replay",
                cursor=tr.ReplayCursor(w1.records))
    with pytest.raises(tr.DivergenceError):
        b3.span_begin("helper.sync", {"kwargs": {"n": 999}})


def test_cell_done_force_closes_dangling_spans():
    reg = make_span_registry()
    w = tr.TraceWriter(run={"id": "sp4"})
    b = Broker(reg, w)
    b.current_cell = "c1"
    b.span_begin("helper.sync", {})
    b.cell_done(cell="c1", ok=False,
                error={"type": "Interrupted", "message": "fuel"}, interrupted=True)
    end, cell = w.records[2], w.records[3]
    assert end["kind"] == "span" and end["phase"] == "end"
    assert end["ok"] is False and end["error"]["type"] == "unclosed_span"
    assert cell["kind"] == "cell"
    assert b._span_stack == []


def test_span_end_without_begin_raises():
    reg = make_span_registry()
    b = Broker(reg, tr.TraceWriter(run={"id": "sp5"}))
    with pytest.raises(EffectError) as ei:
        b.span_end(ok=True)
    assert ei.value.type == "no_open_span"
