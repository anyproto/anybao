"""Golden recall eval (ADR-007 §7.1) + ROI metrics.

Offline: fixture parsing, scoring, ROI math — always on. The live eval
(`-m integration`) seeds the fixture corpus into a fresh space and is
RED when recall@5 drops below 1.0 (index-timing environments skip, per
the integration conventions)."""

import time
from pathlib import Path

import pytest
from anybao import roi
from anybao.anyclient import AnyClient
from anybao.recall import Recall
from anybao.recalleval import EvalReport, load_eval, run_eval, seed_corpus, wait_for_index

FIXTURE = Path(__file__).parent / "fixtures" / "recall-eval.jsonl"


# --- offline ------------------------------------------------------------------

def test_fixture_parses_and_is_self_consistent():
    items, cases = load_eval(FIXTURE)
    assert len(items) >= 10 and len(cases) >= 6
    for it in items:
        assert it["category"] and it["context"]      # ADR-007 §1 requireds
    contexts = [it["context"] for it in items]
    for case in cases:  # every expectContext resolves to exactly one item
        matches = [c for c in contexts if case["expectContext"] in c]
        assert len(matches) == 1, f"{case['query']!r} → {matches}"


def fake_search_client(items_by_query):
    """search returns pointer hits; hydration resolves via /query."""
    all_items = {f"m{i}": it for i, it in enumerate(
        {it["context"]: it for its in items_by_query.values() for it in its}.values())}

    def send(method, path, body):
        if path.endswith("/search"):
            wanted = items_by_query.get(body["query"], [])
            ids = [mid for mid, it in all_items.items() if it in wanted]
            return 200, {"hits": [
                {"scope": "agent", "objectId": "brain", "recordId": mid,
                 "dataset": "agent_memory_items", "score": 0.9} for mid in ids]}
        if path.endswith("/query"):
            ids = body["filter"]["id"]["$in"]
            return 200, {"records": [
                {"id": mid, **all_items[mid]} for mid in ids if mid in all_items]}
        return 404, {"error": {"code": "unknown", "message": path}}
    return AnyClient(send)


def test_run_eval_scores_hits_and_reports_failures():
    hit_item = {"category": "fact", "context": "the sky is blue today"}
    client = fake_search_client({"sky color": [hit_item], "moon phase": []})
    report = run_eval(Recall(client, "s1"),
                      [{"query": "sky color", "expectContext": "sky is blue"},
                       {"query": "moon phase", "expectContext": "waxing"}])
    assert (report.total, report.passed) == (2, 1)
    assert report.recall_rate == 0.5
    assert report.failures[0]["query"] == "moon phase"


def test_empty_report_rate_is_zero():
    assert EvalReport().recall_rate == 0.0


# --- ROI math -----------------------------------------------------------------

def test_referenced_heuristic():
    assert roi.referenced("prefers dark roast coffee", ["Ordered the dark ROAST."])
    assert not roi.referenced("prefers dark roast coffee", ["Done!"])
    assert not roi.referenced("", ["anything"])


def test_log_injection_and_stats():
    written = {}

    def send(method, path, body):
        if path.endswith("/modify"):
            rec = body["records"][0]
            written[rec["id"]] = rec["ops"][0]["value"]
            return 200, {"versionId": "v", "changeId": "c", "recordIds": [rec["id"]]}
        if path.endswith("/query") and body["dataset"] == roi.DATASET:
            return 200, {"records": list(written.values())}
        if path.endswith("/query"):  # agent_memory_items w/ source filter
            return 200, {"records": [
                {"id": "e1", "source": "extraction", "accessCount": 2},
                {"id": "e2", "source": "extraction", "accessCount": 0}]}
        return 404, {"error": {"code": "unknown", "message": path}}

    client = AnyClient(send)
    injected = [({"objectId": "brain"}, {"id": "m1", "context": "dark roast coffee"}),
                ({"objectId": "brain"}, {"id": "m2", "context": "sqlite tracker"})]
    n = roi.log_injection(client, "s1", injected, ["I'll get the dark roast."],
                          ts=1000)
    assert n == 2
    assert written["m1:1000"]["referenced"] is True
    assert written["m2:1000"]["referenced"] is False

    stats = roi.injection_stats(client, "s1", "brain")
    assert stats == {"injected": 2, "referenced": 1, "unreferencedRate": 0.5}
    ex = roi.extraction_stats(client, "s1", "brain")
    assert ex == {"extracted": 2, "recalled": 1, "unrecalledRate": 0.5}


# --- the live golden eval -------------------------------------------------------

@pytest.mark.integration
def test_golden_recall_eval_live(client, fresh_space):
    items, cases = load_eval(FIXTURE)
    seed_corpus(client, fresh_space, items)
    r = Recall(client, fresh_space)
    try:
        ready = wait_for_index(r, cases[0]["query"])
    except Exception as e:
        if "index.disabled" in str(e):
            pytest.skip("search index disabled on this server")
        raise
    if not ready:
        pytest.skip("index not populated — embedder/indexer timing")
    time.sleep(1.0)  # let the tail of the corpus finish indexing
    report = run_eval(r, cases)
    assert report.recall_rate == 1.0, f"recall regressed: {report.failures}"
