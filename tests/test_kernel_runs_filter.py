"""effects.runs() filter: instants become the run summary's seconds (ADR-023)."""

from kernelenv import load_kernel


def test_runs_filter_takes_instants_as_the_summary_seconds():
    # trace_runs stores startedAt as unix seconds; an instant() in the
    # filter used to reach the store as {"$date"} and match nothing
    seen = []

    def eff(name, payload):
        seen.append((name, payload))
        return {"value": None} if name == "runtime.get" else {"runs": []}
    app = load_kernel(effect=eff)
    app.effects.runs(filter={"startedAt": {"$gte": app.instant(1700000000)},
                             "status": {"$in": ["ok"]}})
    q = [p for n, p in seen if n == "trace.runs"][-1]
    assert q["filter"] == {"startedAt": {"$gte": 1700000000.0}, "status": {"$in": ["ok"]}}
