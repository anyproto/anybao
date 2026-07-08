"""Trigger control API — TriggerService over a fake in-memory store,
plus one live round-trip through the stdlib HTTP binding. Offline."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from anybao.trigger_control import (
    TriggerControlServer,
    TriggerNotFound,
    TriggerService,
    start_server,
)
from anybao.triggers import RunRecord, Trigger, rollup, trigger_from_record, trigger_to_record


class FakeStore:
    """In-memory stand-in for triggers.TriggerStore (same surface)."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.run_rows: list[dict] = []

    def save(self, t: Trigger) -> None:
        self.records[t.id] = {"id": t.id, **trigger_to_record(t)}

    def load_all(self) -> list[Trigger]:
        return [trigger_from_record(r) for r in self.records.values()]

    def record_run(self, t: Trigger, run: RunRecord, *, ts_ms: int) -> None:
        self.run_rows.append({"triggerId": t.id, "ts": run.ts, "status": run.status,
                              "durationMs": run.duration_ms})
        self.save(t)

    def runs(self, trigger_id: str, limit: int = 20) -> list[dict]:
        rows = [r for r in self.run_rows if r["triggerId"] == trigger_id]
        return sorted(rows, key=lambda r: -r["ts"])[:limit]


CRON = {"name": "sweep", "kind": "cron", "spec": {"every_s": 60}, "program": "sweep@v1"}
EVENT = {"name": "watch", "kind": "event", "spec": {"dataset": "chat_messages"},
         "program": "runner@v1"}


def svc(store: FakeStore | None = None) -> TriggerService:
    return TriggerService(store if store is not None else FakeStore(), owner="inst-A")


# --- create -------------------------------------------------------------------

def test_create_assigns_id_and_stamps_owner():
    store = FakeStore()
    s = svc(store)
    tid = s.create(dict(CRON))
    assert tid
    rec = s.get(tid)
    assert rec["owner"] == "inst-A"
    assert store.records[tid]["id"] == tid  # persisted via the store


def test_create_keeps_explicit_owner():
    s = svc()
    tid = s.create({**EVENT, "owner": "inst-B"})
    assert s.get(tid)["owner"] == "inst-B"


def test_create_validates_definition():
    s = svc()
    with pytest.raises(ValueError, match="kind"):
        s.create({**CRON, "kind": "webhook"})
    with pytest.raises(ValueError, match="program"):
        s.create({"name": "x", "kind": "cron", "spec": {"every_s": 5}})
    with pytest.raises(ValueError, match="cron"):
        s.create({**CRON, "spec": {}})
    with pytest.raises(ValueError, match="dataset"):
        s.create({**EVENT, "spec": {}})


def test_create_rejects_unknown_and_service_owned_fields():
    s = svc()
    with pytest.raises(ValueError, match="unknown"):
        s.create({**CRON, "nam": "typo"})
    with pytest.raises(ValueError, match="unknown"):
        s.create({**CRON, "runCount": 7})  # rollup is service-owned


def test_loads_existing_triggers_from_store():
    store = FakeStore()
    tid = svc(store).create(dict(CRON))
    s2 = TriggerService(store, owner="inst-A")
    assert s2.get(tid)["name"] == "sweep"


# --- list / get ----------------------------------------------------------------

def test_list_returns_rollup_views():
    store = FakeStore()
    s = svc(store)
    tid = s.create(dict(CRON))
    s.create(dict(EVENT))
    views = s.list()
    assert len(views) == 2
    view = next(v for v in views if v["id"] == tid)
    assert view == rollup(trigger_from_record(store.records[tid]))
    assert "failureRate" in view and "lastStatus" in view and "limits" in view
    assert "spec" not in view  # monitoring view, not the full definition


def test_get_unknown_id_raises_not_found():
    with pytest.raises(TriggerNotFound):
        svc().get("nope")


# --- enable / disable / patch / delete ------------------------------------------

def test_disable_then_enable_persists():
    store = FakeStore()
    s = svc(store)
    tid = s.create(dict(CRON))
    assert s.disable(tid)["enabled"] is False
    assert store.records[tid]["enabled"] is False
    assert s.enable(tid)["enabled"] is True
    assert store.records[tid]["enabled"] is True


def test_enable_resets_circuit_breaker():
    store = FakeStore()
    tid = svc(store).create(dict(CRON))
    # simulate a synced auto_disabled trigger (circuit breaker tripped elsewhere)
    store.records[tid].update(enabled=False, lastStatus="auto_disabled",
                              consecutiveFailures=3, runCount=5)
    s = TriggerService(store, owner="inst-A")
    rec = s.enable(tid)
    assert rec["enabled"] is True
    assert rec["consecutiveFailures"] == 0
    assert rec["runCount"] == 5  # rollup history stays


def test_patch_updates_fields_and_persists():
    store = FakeStore()
    s = svc(store)
    tid = s.create(dict(CRON))
    rec = s.patch(tid, {"name": "sweep2", "spec": {"every_s": 5},
                        "limits": {"fuelPerRun": 100}})
    assert rec["name"] == "sweep2"
    assert rec["spec"] == {"every_s": 5}
    assert store.records[tid]["limits"] == {"fuelPerRun": 100}


def test_patch_rejects_unknown_field_and_bad_definition():
    s = svc()
    tid = s.create(dict(CRON))
    with pytest.raises(ValueError, match="unknown"):
        s.patch(tid, {"lastStatus": "ok"})
    with pytest.raises(ValueError, match="cron"):
        s.patch(tid, {"spec": {"dataset": "x"}})  # invalid for kind=cron
    assert s.get(tid)["spec"] == CRON["spec"]  # failed patch left it untouched


def test_patch_unknown_id_raises_not_found():
    with pytest.raises(TriggerNotFound):
        svc().patch("nope", {"name": "x"})


def test_delete_removes_trigger():
    s = svc()
    tid = s.create(dict(CRON))
    s.delete(tid)
    with pytest.raises(TriggerNotFound):
        s.get(tid)
    with pytest.raises(TriggerNotFound):
        s.delete(tid)


# --- runs -----------------------------------------------------------------------

def test_runs_returns_newest_first_with_limit():
    store = FakeStore()
    s = svc(store)
    tid = s.create(dict(CRON))
    for i in range(3):
        store.run_rows.append({"triggerId": tid, "ts": 1000.0 + i, "status": "ok",
                               "durationMs": 5})
    store.run_rows.append({"triggerId": "other", "ts": 9999.0, "status": "ok",
                           "durationMs": 1})
    rows = s.runs(tid, limit=2)
    assert [r["ts"] for r in rows] == [1002.0, 1001.0]
    with pytest.raises(TriggerNotFound):
        s.runs("nope", limit=2)


# --- HTTP binding ---------------------------------------------------------------

def _req(method: str, url: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def base_url():
    srv = start_server(svc())
    assert isinstance(srv, TriggerControlServer)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_round_trip(base_url):
    status, out = _req("POST", f"{base_url}/triggers", dict(CRON))
    assert status == 201
    assert isinstance(out, dict)
    tid = out["id"]

    status, views = _req("GET", f"{base_url}/triggers")
    assert status == 200
    assert isinstance(views, list) and views[0]["id"] == tid

    status, rec = _req("PATCH", f"{base_url}/triggers/{tid}", {"name": "renamed"})
    assert status == 200
    assert isinstance(rec, dict) and rec["name"] == "renamed"

    status, rec = _req("POST", f"{base_url}/triggers/{tid}/disable")
    assert status == 200
    assert isinstance(rec, dict) and rec["enabled"] is False

    status, rows = _req("GET", f"{base_url}/triggers/{tid}/runs?limit=5")
    assert status == 200 and rows == []

    status, _ = _req("DELETE", f"{base_url}/triggers/{tid}")
    assert status == 204

    status, err = _req("GET", f"{base_url}/triggers/{tid}")
    assert status == 404
    assert isinstance(err, dict) and err["error"]["code"] == "trigger.not_found"


def test_http_errors(base_url):
    status, err = _req("POST", f"{base_url}/triggers", {"kind": "webhook"})
    assert status == 400
    assert isinstance(err, dict) and err["error"]["code"] == "request.invalid"

    status, err = _req("GET", f"{base_url}/nope")
    assert status == 404
    assert isinstance(err, dict) and err["error"]["code"] == "route.not_found"
