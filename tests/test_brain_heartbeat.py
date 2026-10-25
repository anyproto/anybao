"""programs/brainHeartbeat@v1 — the brain's v0 heartbeat, under the REAL
guest kernel (kernelenv). Pinned: unconfigured args raise with no writes;
a missing or unreadable exclusion list writes one refused signal and
raises; a fresh tick writes no gap; an old last tick writes one
heartbeat_gap signal; every successful run records its tick."""

import pytest
from kernelenv import load_kernel

SPACE, HUB = "bao-space", "hub"
DATASETS = {"signals": "hub_signals", "config": "hub_config",
            "incidents": "hub_incidents", "observations": "hub_observations"}
ARGS = {"space": SPACE, "hub": HUB, "datasets": DATASETS}
NOW = 1_800_000_000.0
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}


class FakeAny:
    def __init__(self, config=None):
        self.config = {rid: {"id": rid, "value": v} for rid, v in (config or {}).items()}
        self.signals = {}
        self.writes = []

    def query(self, space, oid, ds, filter=None, limit=None, **_):
        assert (space, oid, ds) == (SPACE, HUB, DATASETS["config"])
        # a shared dataset: an id filter compares "<objectId>/<recordId>"
        want = (filter or {}).get("id") or ""
        prefix = f"{oid}/"
        rec = self.config.get(want[len(prefix):]) if want.startswith(prefix) else None
        return [{**rec, "id": f"{oid}/{rec['id']}"}] if rec else []

    def upsert_record(self, space, oid, ds, rid, value):
        assert (space, oid) == (SPACE, HUB)
        self.writes.append((ds, rid))
        if ds == DATASETS["signals"]:
            self.signals[rid] = value
        else:
            assert ds == DATASETS["config"]
            self.config[rid] = {"id": rid, **value}
        return {}


def run(args, config=None):
    fake = FakeAny(config)

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": NOW}
        raise AssertionError(f"unexpected effect {name}")

    mod = load_kernel(effect=eff, any_client=fake).use("brainHeartbeat@v1")
    return mod, fake


def test_unconfigured_args_raise_with_no_writes():
    mod, fake = run({})
    with pytest.raises(Exception) as err:
        mod.main({"space": SPACE})
    assert type(err.value).__name__ == "unconfigured"
    assert fake.writes == []


def test_a_missing_exclusion_list_is_refused_and_raises():
    mod, fake = run(ARGS)
    with pytest.raises(Exception) as err:
        mod.main(ARGS)
    assert type(err.value).__name__ == "exclusions_unreadable"
    (signal,) = fake.signals.values()
    assert signal["kind"] == "exclusions_unreadable" and signal["decision"] == "refused"
    assert signal["subject"] == "agent:brainHeartbeat@v1"
    assert "heartbeat" not in fake.config


def test_a_wrong_version_is_refused():
    mod, fake = run(ARGS, {"exclusions": {**EXCLUSIONS, "version": 2}})
    with pytest.raises(Exception, match="exclusion list version 2"):
        mod.main(ARGS)
    assert "version 2" in next(iter(fake.signals.values()))["reason"]


def test_a_first_tick_writes_no_gap_and_records_itself():
    mod, fake = run(ARGS, {"exclusions": EXCLUSIONS})
    assert mod.main(ARGS) == {"ok": True, "gap_s": None}
    assert fake.signals == {}
    assert fake.config["heartbeat"]["value"] == {"lastTickAt": NOW}


def test_an_old_last_tick_writes_one_gap_signal():
    mod, fake = run(ARGS, {"exclusions": EXCLUSIONS,
                           "heartbeat": {"lastTickAt": NOW - 1300}})
    assert mod.main(ARGS)["gap_s"] == 1300
    (signal,) = fake.signals.values()
    assert signal["kind"] == "heartbeat_gap" and signal["decision"] == "none"
    assert signal["reason"] == "no tick for 1300s"


def test_a_recent_last_tick_writes_none():
    mod, fake = run(ARGS, {"exclusions": EXCLUSIONS,
                           "heartbeat": {"lastTickAt": NOW - 500}})
    mod.main(ARGS)
    assert fake.signals == {}
