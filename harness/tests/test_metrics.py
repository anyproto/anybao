"""Metrics tuning report (implementation-plan checkpoint; ADR-003/005)
— synthetic traces via TraceWriter, pure aggregation, offline."""

from anybao.metrics import dist, percentile, report, scan, scan_run
from anyrt import trace as tr


def llm_effect(w, messages, usage, parts=None, stop="done"):
    w.effect(
        effect="llm.chat", cell=None,
        input={"messages": messages, "tier": "codegen"},
        key=tr.input_key("llm.chat", {"messages": messages, "tier": "codegen"}),
        output={"parts": parts or [{"type": "text", "text": "ok"}],
                "stop": stop, "usage": usage},
        meta={"class": "read", "usage": usage},
    )


def make_run(run_id, *, fuel, digest) -> tr.TraceWriter:
    """Two turns; turn 2's input repeats turn 1's tool_result (the
    O(turns²) growth) — the digest must be counted ONCE."""
    w = tr.TraceWriter(run={"id": run_id, "program": "toolcaller"})
    user = {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
    llm_effect(w, [user], {"in": 100, "out": 20}, stop="tool", parts=[
        {"type": "tool_call", "id": "cell_1", "name": "run_cell",
         "args": {"code": "1+1"}},
    ])
    w.effect(
        effect="fetch", cell="cell_1",
        input={"url": "http://x"}, key=tr.input_key("fetch", {"url": "http://x"}),
        output={"status": 200}, meta={"durMs": 5, "class": "read"},
    )
    w.cell(cell="cell_1", ok=True,
           metrics={"fuel_used": fuel, "duration_ms": 40, "mem_pages": 8})
    result = {"role": "user", "parts": [
        {"type": "tool_result", "call_id": "cell_1", "content": "x" * digest},
    ]}
    llm_effect(w, [user, result], {"in": 200, "out": 30})
    return w


def test_scan_run_folds_one_trace():
    r = scan_run(make_run("run_a", fuel=1000, digest=80).records)
    assert r["run"] == "run_a" and r["program"] == "toolcaller"
    assert r["cells"] == 1
    assert r["fuel"] == [1000]
    assert r["cell_duration_ms"] == [40]
    assert r["effects"] == {"llm.chat": 2, "fetch": 1}
    assert r["tokens_in"] == [100, 200] and r["tokens_out"] == [20, 30]
    # tool_result repeated across turn inputs -> counted once
    assert r["digest_bytes"] == [80]


def test_scan_directory_skips_non_traces(tmp_path):
    make_run("run_a", fuel=1000, digest=80).dump(tmp_path / "run_a.jsonl")
    make_run("run_b", fuel=3000, digest=200).dump(tmp_path / "run_b.jsonl")
    (tmp_path / "junk.jsonl").write_text("not json\n")
    runs = scan(tmp_path)
    assert [r["run"] for r in runs] == ["run_a", "run_b"]


def test_scan_hydrates_blob_spilled_inputs(tmp_path):
    w = make_run("run_big", fuel=1, digest=200_000)  # spills over 64 KB
    path = tmp_path / "run_big.jsonl"
    w.dump(path)
    assert w.blobs and path.with_suffix(".jsonl.blobs").exists()
    (r,) = scan(tmp_path)
    assert r["digest_bytes"] == [200_000]


def test_percentile_and_dist():
    assert percentile([], 95) is None
    assert percentile([7], 50) == 7
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 0) == 1
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 100) == 100
    assert dist(values) == {"n": 100, "min": 1, "p50": 50, "p95": 95, "max": 100}
    assert dist([]) == {"n": 0, "min": None, "p50": None, "p95": None, "max": None}


def test_report_distributions_and_suggestions(tmp_path):
    make_run("run_a", fuel=1000, digest=80).dump(tmp_path / "run_a.jsonl")
    make_run("run_b", fuel=3000, digest=400).dump(tmp_path / "run_b.jsonl")
    rep = report(scan(tmp_path))
    assert rep["runs"] == 2 and rep["cells"] == 2 and rep["turns"] == 4
    assert rep["tokens_in"] == 600 and rep["tokens_out"] == 100
    assert rep["effects"] == {"llm.chat": 4, "fetch": 2}
    assert rep["fuel"] == {"n": 2, "min": 1000, "p50": 1000, "p95": 3000, "max": 3000}
    assert rep["tokens_per_turn"]["p50"] == 120 and rep["tokens_per_turn"]["max"] == 230
    s = rep["suggest"]
    assert s["fuel_per_cell"] == 2 * 3000              # 2x p95
    assert s["cell_timeout_s"] == 1                    # 2x 40ms, floored to 1s
    assert s["digest_inline_token_budget"] == 100      # ceil(400 / 4)


def test_report_empty_is_all_nones():
    rep = report([])
    assert rep["runs"] == 0
    assert rep["fuel"]["p95"] is None
    assert rep["suggest"] == {}
