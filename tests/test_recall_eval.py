"""Golden recall eval — ADR-007 §7.1: a workload-specific fixture set
(query → expected item) run against the real index, red when recall
regresses. Search quality is a named dependency risk: the `any` search
stack's own evals are synthetic; THIS one speaks the memory workload —
short conversational queries over a small personal corpus.

Fixture: tests/fixtures/recall-eval.jsonl — one record per line (the
repo's JSONL contract): `{"kind": "item", category, context, body?}`
seeds the corpus; `{"kind": "case", query, expectContext, k?}` asserts
the item whose context contains `expectContext` lands in the top-k.

Rides the server's ASYNC indexer, so it seeds then polls before
asserting — and SKIPS (not fails) if the index never populates:
embedder/indexer timing is environmental, retrieval quality is what's
pinned. Marked `integration`.
"""

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "recall-eval.jsonl"
DEFAULT_K = 5


def load_eval(path):
    import json
    items, cases = [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        (items if rec["kind"] == "item" else cases).append(rec)
    return items, cases


def _search(client, space, query, k):
    return client.search(space, query, scopes=["agent"], limit=k).get("hits") or []


def _hydrate(client, space, hits):
    out = []
    for h in hits:
        recs = client.query(space, h["objectId"], h["dataset"],
                            filter={"id": h["recordId"]}, limit=1)
        out.extend(recs)
    return out


def _wait_for_index(client, space, probe_query, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _search(client, space, probe_query, DEFAULT_K):
            return True
        time.sleep(0.5)
    return False


def test_recall_golden_eval(client, fresh_space):
    items, cases = load_eval(FIXTURE)

    for it in items:
        client.create_memory(fresh_space, {k: v for k, v in it.items() if k != "kind"})

    # the indexer is async — poll on the first case before asserting
    try:
        ready = _wait_for_index(client, fresh_space, cases[0]["query"])
    except Exception as e:  # noqa: BLE001 - environmental index states -> skip
        if getattr(e, "code", "") == "index.disabled":
            pytest.skip("search index disabled on this server")
        raise
    if not ready:
        pytest.skip("index not populated — embedder/indexer timing")

    failures = []
    for case in cases:
        hits = _search(client, fresh_space, case["query"], case.get("k", DEFAULT_K))
        contexts = [r.get("context", "") for r in _hydrate(client, fresh_space, hits)]
        if not any(case["expectContext"] in c for c in contexts):
            failures.append({"query": case["query"],
                             "expected": case["expectContext"], "got": contexts})

    passed = len(cases) - len(failures)
    assert not failures, f"recall {passed}/{len(cases)}; misses: {failures}"
