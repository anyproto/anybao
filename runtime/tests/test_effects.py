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
