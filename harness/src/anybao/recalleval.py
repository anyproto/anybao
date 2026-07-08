"""Golden recall eval — ADR-007 §7.1: a workload-specific fixture set
(query → expected item) run against the real index, red in CI when
recall regresses. Search quality is a named dependency risk: the
`any` search stack's own evals are synthetic; THIS one speaks the
memory workload — short conversational queries over a small personal
corpus.

Fixture: harness/tests/fixtures/recall-eval.jsonl — one record per line
(the repo's JSONL contract): `{"kind": "item", category, context,
body?}` seeds the corpus; `{"kind": "case", query, expectContext, k?}`
asserts the item whose context contains `expectContext` lands in the
top-k. Re-seed from a fresh bao export by regenerating item lines —
cases keep working as long as expectContext substrings survive.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .anyclient import AnyClient

DEFAULT_K = 5


@dataclass
class EvalReport:
    total: int = 0
    passed: int = 0
    failures: list[dict] = field(default_factory=list)

    @property
    def recall_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def load_eval(path: str | Path) -> tuple[list[dict], list[dict]]:
    items, cases = [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        (items if rec["kind"] == "item" else cases).append(rec)
    return items, cases


def seed_corpus(client: AnyClient, space: str, items: list[dict]) -> None:
    for it in items:
        body = {k: v for k, v in it.items() if k != "kind"}
        client.create_memory(space, body)


def _search(client: AnyClient, space: str, query: str, k: int) -> list[dict]:
    return client.search(space, query, scopes=["agent"], limit=k).get("hits") or []


def _hydrate(client: AnyClient, space: str, hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        recs = client.query(space, h["objectId"], h["dataset"],
                            filter={"id": h["recordId"]}, limit=1)
        out.extend(recs)
    return out


def wait_for_index(client: AnyClient, space: str, probe_query: str, *,
                   timeout: float = 15.0) -> bool:
    """The indexer is async — poll until the probe surfaces (or give
    up: timing is environmental, the caller decides skip-vs-fail)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _search(client, space, probe_query, DEFAULT_K):
            return True
        time.sleep(0.5)
    return False


def run_eval(client: AnyClient, space: str, cases: list[dict], *,
             k: int = DEFAULT_K) -> EvalReport:
    report = EvalReport()
    for case in cases:
        report.total += 1
        hits = _search(client, space, case["query"], case.get("k", k))
        contexts = [r.get("context", "")
                    for r in _hydrate(client, space, hits)]
        if any(case["expectContext"] in c for c in contexts):
            report.passed += 1
        else:
            report.failures.append({"query": case["query"],
                                    "expected": case["expectContext"],
                                    "got": contexts})
    return report
