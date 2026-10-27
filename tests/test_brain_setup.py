"""programs/brainSetup@v1 — the AI brain's setup run, its reconcile run and
its chat tools, under the REAL guest kernel (kernelenv). Pinned: a run with
no request writes the default and sets the brain up in Bao's space; the
status echoes the request's stamp; a second run only reads; a 409
bundle.not_ready is retried; a failure is written as an error and raised;
off disables the jobs and remembers which, on re-enables exactly those; the
reconcile run rewrites a missing job (under a fresh id when the old one is
burned) and never re-enables a paused one; the chat tools write the
contract rows and re-arm brain-setup; nothing is ever deleted."""

import pytest
from kernelenv import load_kernel

BAO = "bao-space"
NOW = 1_800_000_000.0
STAMP = "2026-10-25T10:00:00.000Z"
TRIGGERS = ("bao-space", "trigger-anchor", "agent_triggers")


class ConfigError(Exception):
    pass


class NotReady(Exception):
    status = 409
    code = "bundle.not_ready"


class FakeAny:
    """Bao's space as the setup run sees it: datasets keyed by
    (space, object, dataset), a catalog, bundles and types."""

    def __init__(self):
        self.records = {}
        self.burned = set()
        self.calls = []
        self.apps = []
        self.types = {}
        self.datasets = {}
        self.not_ready = {}
        self.fail = {}
        self.reject = {}

    # the store
    def _store(self, space, oid, ds):
        return self.records.setdefault((space, oid, ds), {})

    def _maybe_fail(self, method):
        if method in self.fail:
            raise self.fail[method]
        left = self.not_ready.get(method, 0)
        if left:
            self.not_ready[method] = left - 1
            raise NotReady("409 bundle.not_ready")

    def bao_space(self):
        return BAO

    @staticmethod
    def _shared(ds):
        # the hub's stores are declared shared: reads return, and id
        # filters compare, "<objectId>/<recordId>" (any 03-api § Shared datasets)
        return ds.startswith("type-hub_")

    def query(self, space, oid, ds, filter=None, limit=None, **_):
        rows = [({**r, "id": f"{oid}/{r['id']}"} if self._shared(ds) else r)
                for r in self._store(space, oid, ds).values()]
        want = (filter or {}).get("id")
        if want is not None:
            rows = [r for r in rows if r["id"] == want]
        return rows[:limit] if limit else rows

    def upsert_record(self, space, oid, ds, rid, value):
        if (space, oid, ds, rid) in self.burned:
            # the server's shape: HTTP 200, the id even listed, the refusal
            # only in `rejections`
            return {"recordIds": [rid], "rejections": [
                {"recordId": rid, "reason": "crdt: record is deleted; the id cannot be reused"}]}
        if (space, oid, ds, rid) in self.reject:
            return {"recordIds": [], "rejections": [
                {"recordId": rid, "reason": self.reject[(space, oid, ds, rid)]}]}
        self.calls.append(("upsert", ds, rid))
        self._store(space, oid, ds)[rid] = {"id": rid, **value}
        return {}

    def modify(self, space, body):
        store = self._store(space, body["objectId"], body["dataset"])
        for r in body["records"]:
            for op in r["ops"]:
                assert op["type"] == "$set" and op["path"] == "enabled"
                store[r["id"]]["enabled"] = op["value"]
            self.calls.append(("enabled", r["id"], r["ops"][0]["value"]))
        return {}

    def delete(self, space, oid, ds, rid):
        """Test-only: what a user deleting a job by hand does."""
        del self._store(space, oid, ds)[rid]
        self.burned.add((space, oid, ds, rid))

    # provisioning
    def setup_app(self, space, usecase):
        self._maybe_fail("setup_app")
        self.calls.append(("setup_app", usecase))
        self.apps.append(usecase)
        return [{"usecase": usecase, "installed": True}]

    def ensure_bundle(self, space, bundle_id, name=None, root_type=None, **_):
        self._maybe_fail("ensure_bundle")
        self.calls.append(("ensure_bundle", bundle_id, name, root_type))
        return {"bundle": {"id": bundle_id, "rootId": "brain-root"}, "installed": True}

    def create_type(self, space, body):
        self.calls.append(("create_type", body["xKey"]))
        self.types[body["xKey"]] = body
        return {"typeId": "type-hub", "xKey": body["xKey"], "created": True}

    def _create_dataset(self, space, type_key, draft):
        self._maybe_fail("create_dataset")
        self.calls.append(("create_dataset", draft["key"]))
        self.datasets[draft["key"]] = draft
        return {"collection": f"type-hub_{draft['key']}", "created": True}

    def bundle_child(self, space, bundle_id, seed, type_key=None, **_):
        self._maybe_fail("bundle_child")
        if seed == "bao/triggers/v1":
            return {"objectId": "trigger-anchor"}
        assert (bundle_id, seed, type_key) == ("brain/v1", "brain/hub/v1", "brain_hub")
        return {"objectId": "hub"}

    def list_apps(self, space):
        return [{"usecase": u} for u in self.apps]

    def query_objects(self, space, normalize=True, filter=None, limit=None, **_):
        return [{"id": "mb1", "any": {"name": "me@example.com"}}]


def kernel(config=None, fake=None):
    fake = fake or FakeAny()
    cfg = {"llm.tier.codegen": {"model": "codegen-model"},
           "llm.tier.classify": {"model": "classify-model"}}
    cfg.update(config or {})
    writes = []
    sleeps = []

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": NOW}
        if name == "config.get":
            if payload["key"] not in cfg:
                raise ConfigError(f"no config value for {payload['key']!r}")
            return {"value": cfg[payload["key"]]}
        if name == "config.set":
            cfg[payload["key"]] = payload["value"]
            writes.append((payload["key"], payload["value"]))
            return {}
        if name == "sleep":
            sleeps.append(payload["seconds"])
            return {}
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=eff, any_client=fake)
    return app.use("brainSetup@v1"), fake, cfg, writes, sleeps


def statuses(writes):
    return [v for k, v in writes if k == "brain.status"]


def triggers(fake):
    return fake.records.get(TRIGGERS, {})


def jobs(fake):
    return {rid: r for rid, r in triggers(fake).items() if r.get("kind") == "cron"}


def on_request(stamp=STAMP):
    return {"brain.space": {"spaceId": BAO, "requestedAt": stamp, "by": "any-ui"}}


def test_a_run_with_no_request_writes_the_default_and_sets_up():
    mod, fake, cfg, writes, _ = kernel()
    out = mod.main({"requestedAt": "ignored"})
    request = cfg["brain.space"]
    assert request["spaceId"] == BAO and request["by"] == "default"
    assert request["requestedAt"].endswith("Z")
    assert [s["state"] for s in statuses(writes)] == ["provisioning", "ready"]
    assert all(s["requestedAt"] == request["requestedAt"] for s in statuses(writes))
    assert out["state"] == "ready"


def test_setup_installs_the_apps_bundle_hub_and_seeds():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({"requestedAt": STAMP})
    assert ("setup_app", "people") in fake.calls and ("setup_app", "meetings") in fake.calls
    assert ("ensure_bundle", "brain/v1", "Brain", "page") in fake.calls
    assert fake.types["brain_hub"]["hidden"] is True and fake.types["brain_hub"]["body"] is False
    assert sorted(fake.datasets) == ["config", "incidents", "observations", "signals"]
    signals = fake.datasets["signals"]
    assert signals["shared"] is True and signals["search"]["scope"] == "signals"
    required = {f["key"] for f in signals["fields"] if f.get("required")}
    assert required == {"tick", "kind", "subject", "decision", "reason"}
    exclusions = fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"]
    assert exclusions == {"version": 1, "personIds": [], "emails": [], "domains": []}
    assert cfg["llm.tier.brain.drafter"] == {"model": "codegen-model"}
    assert cfg["llm.tier.brain.extractor"] == {"model": "codegen-model"}
    assert cfg["llm.tier.brain.caretaker"] == {"model": "classify-model"}
    ready = statuses(writes)[-1]
    assert ready["hub"] == {"objectId": "hub", "datasets": {
        "signals": "type-hub_signals", "config": "type-hub_config",
        "incidents": "type-hub_incidents", "observations": "type-hub_observations"}}


def test_setup_writes_the_jobs_unowned_and_enabled():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    js = jobs(fake)
    assert sorted(js) == ["brain-heartbeat", "brain-reconcile"]
    beat = js["brain-heartbeat"]
    assert beat["program"] == "agent:brainHeartbeat@v1" and beat["spec"] == {"every_s": 600}
    assert beat["args"]["space"] == BAO and beat["args"]["hub"] == "hub"
    assert beat["args"]["datasets"]["config"] == "type-hub_config"
    assert js["brain-reconcile"]["args"] == {"mode": "reconcile"}
    assert all(j["enabled"] is True and "owner" not in j for j in js.values())


def test_a_second_run_only_reads():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    before = list(fake.calls)
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "2026-10-25T11:00:00.000Z",
                          "by": "any-ui"}
    mod.main({})
    new = fake.calls[len(before):]
    assert not [c for c in new if c[0] in ("upsert", "enabled")]
    assert [k for k, _ in writes].count("llm.tier.brain.drafter") == 1


def test_an_edited_tier_is_never_overwritten():
    mine = {"model": "my-own-model"}
    mod, _, cfg, _, _ = kernel(config={**on_request(), "llm.tier.brain.drafter": mine})
    mod.main({})
    assert cfg["llm.tier.brain.drafter"] == mine


def test_bundle_not_ready_is_retried():
    fake = FakeAny()
    fake.not_ready["bundle_child"] = 2
    mod, _, _, writes, sleeps = kernel(config=on_request(), fake=fake)
    mod.main({})
    assert sleeps == [2, 2]
    assert statuses(writes)[-1]["state"] == "ready"


def test_a_failure_is_written_as_an_error_and_raised():
    fake = FakeAny()
    fake.fail["create_dataset"] = ValueError("the server refused the dataset")
    mod, _, _, writes, _ = kernel(config=on_request(), fake=fake)
    with pytest.raises(Exception) as err:
        mod.main({})
    assert type(err.value).__name__ == "provision_failed"
    error = statuses(writes)[-1]
    assert error["state"] == "error" and error["requestedAt"] == STAMP
    assert "the server refused the dataset" in error["error"]["message"]
    assert jobs(fake) == {}


def test_a_request_for_another_space_is_an_error():
    mod, _, _, writes, _ = kernel(config={"brain.space": {
        "spaceId": "elsewhere", "requestedAt": STAMP, "by": "any-ui"}})
    with pytest.raises(Exception, match="the request named another"):
        mod.main({})
    assert statuses(writes)[-1]["error"]["type"] == "wrong_space"


def test_off_disables_and_on_re_enables_exactly_those():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    # the user paused the heartbeat in Scheduled before switching off
    triggers(fake)["brain-heartbeat"]["enabled"] = False
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    out = mod.main({})
    assert out["disabled"] == ["brain-reconcile"]
    off = statuses(writes)[-1]
    assert off == {"spaceId": None, "requestedAt": "s-off", "state": "off", "at": off["at"]}
    assert all(not j["enabled"] for j in jobs(fake).values())
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    out = mod.main({})
    assert out["reenabled"] == ["brain-reconcile"]
    assert jobs(fake)["brain-reconcile"]["enabled"] is True
    assert jobs(fake)["brain-heartbeat"]["enabled"] is False  # the pause held


def test_off_after_a_failed_setup_still_brings_the_jobs_back():
    # an error status carries no hub, so the jobs an off run disables must
    # be remembered somewhere that does not need one
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-retry", "by": "any-ui"}
    fake.fail["setup_app"] = RuntimeError("catalog unavailable")
    with pytest.raises(Exception, match="catalog unavailable"):
        mod.main({})
    assert statuses(writes)[-1]["state"] == "error"
    del fake.fail["setup_app"]
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    mod.main({})
    assert all(not j["enabled"] for j in jobs(fake).values())
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    assert mod.main({})["state"] == "ready"
    assert all(j["enabled"] for j in jobs(fake).values())


def test_a_stopped_job_is_a_warning_on_a_ready_brain_that_keeps_working():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    ready = statuses(writes)[-1]
    triggers(fake)["brain-heartbeat"].update(enabled=False, lastStatus="auto_disabled",
                                              consecutiveFailures=3)
    mod.main({"mode": "reconcile"})
    status = cfg["brain.status"]
    assert status["state"] == "ready" and status["hub"] == ready["hub"]
    assert status["at"] == ready["at"]
    assert status["stopped"] == [{"id": "brain-heartbeat", "name": "brain heartbeat"}]
    assert mod.status()["state"] == "ready"
    assert mod.status()["stopped"] == [{"id": "brain-heartbeat", "name": "brain heartbeat"}]
    mod.exclude(email="boss@example.com")          # the brain's tools still answer
    n = len(writes)
    mod.main({"mode": "reconcile"})                # nothing changed: nothing written
    assert len(writes) == n
    triggers(fake)["brain-heartbeat"]["enabled"] = True    # resumed in Scheduled
    mod.main({"mode": "reconcile"})
    assert "stopped" not in cfg["brain.status"] and cfg["brain.status"]["state"] == "ready"


def test_try_again_restarts_a_stopped_job_but_not_a_paused_one():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake)["brain-heartbeat"].update(enabled=False, lastStatus="auto_disabled")
    triggers(fake)["brain-reconcile"]["enabled"] = False      # the user paused it
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-retry", "by": "any-ui"}
    out = mod.main({})
    assert out["state"] == "ready" and out["restarted"] == ["brain-heartbeat"]
    assert jobs(fake)["brain-heartbeat"]["enabled"] is True
    assert jobs(fake)["brain-reconcile"]["enabled"] is False
    assert "stopped" not in cfg["brain.status"]


def test_a_setup_run_for_the_same_request_restarts_nothing():
    # a chat-scan change re-arms setup on the request it already answered;
    # only a new request (Try again) restarts a stopped job
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake)["brain-heartbeat"].update(enabled=False, lastStatus="auto_disabled")
    out = mod.main({})
    assert out["restarted"] == [] and jobs(fake)["brain-heartbeat"]["enabled"] is False
    assert cfg["brain.status"]["stopped"] == [{"id": "brain-heartbeat", "name": "brain heartbeat"}]


def test_reconcile_rewrites_a_deleted_job_under_a_fresh_id():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    fake.delete(*TRIGGERS, "brain-heartbeat")
    out = mod.main({"mode": "reconcile"})
    (new_id,) = out["written"]
    assert new_id.startswith("brain-heartbeat-")
    assert jobs(fake)[new_id]["program"] == "agent:brainHeartbeat@v1"


def test_a_rejected_write_fails_the_run_instead_of_passing_for_done():
    fake = FakeAny()
    fake.reject[(BAO, "hub", "type-hub_config", "exclusions")] = "value invalid"
    mod, _, _, writes, _ = kernel(config=on_request(), fake=fake)
    with pytest.raises(Exception, match="seeding the exclusion list: value invalid"):
        mod.main({})
    assert statuses(writes)[-1]["state"] == "error"


def test_reconcile_never_re_enables_a_paused_job():
    mod, fake, _, writes, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake)["brain-heartbeat"]["enabled"] = False
    out = mod.main({"mode": "reconcile"})
    assert statuses(writes)[-1]["state"] == "ready"     # a pause is not a failure
    assert out == {"reconciled": True, "written": [], "stopped": []}
    assert jobs(fake)["brain-heartbeat"]["enabled"] is False


def test_a_users_exclusions_survive_another_setup_run():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    mod.exclude(email="boss@example.com")
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-again", "by": "any-ui"}
    mod.main({})
    stored = fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"]
    assert stored["emails"] == ["boss@example.com"]


def test_reconcile_waits_for_an_answered_ready_brain():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    out = mod.main({"mode": "reconcile"})
    assert out["reconciled"] is False and jobs(fake) == {}


def test_reconcile_keeps_an_off_brain_off():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    mod.main({})
    triggers(fake)["brain-reconcile"]["enabled"] = True   # someone flipped it back
    out = mod.main({"mode": "reconcile"})
    assert out["disabled"] == ["brain-reconcile"]


def test_on_and_off_from_chat_write_the_request_and_re_arm_setup():
    mod, fake, cfg, _, _ = kernel()
    out = mod.off()
    assert out["state"] == "waiting"
    assert cfg["brain.space"]["spaceId"] is None and cfg["brain.space"]["by"] == "chat"
    armed = triggers(fake)["brain-setup"]
    assert armed["kind"] == "once" and armed["program"] == "agent:brainSetup@v1"
    assert armed["spec"] == {"at": NOW} and armed["enabled"] is True
    # a fired once keeps its lastRunAt through a merge-$set: the re-arm clears it
    assert armed["lastRunAt"] is None
    assert armed["args"] == {"requestedAt": cfg["brain.space"]["requestedAt"]}
    mod.on()
    assert cfg["brain.space"]["spaceId"] == BAO


def test_exclude_appends_normalized_and_never_removes():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    mod.exclude(email=" Boss@Example.com ")
    out = mod.exclude(domain="example.org", email="boss@example.com")
    assert out["emails"] == ["boss@example.com"] and out["domains"] == ["example.org"]
    assert out["version"] == 1


def test_exclude_refuses_before_setup():
    mod, _, _, _, _ = kernel()
    with pytest.raises(Exception) as err:
        mod.exclude(email="a@b.c")
    assert type(err.value).__name__ == "not_set_up"


def test_set_chat_spaces_writes_the_row_and_re_arms():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    assert mod.set_chat_spaces(["s2", "s1", "s2"]) == {"spaces": ["s1", "s2"]}
    assert cfg["brain.chatScan.spaces"] == ["s1", "s2"]
    assert triggers(fake)["brain-setup"]["args"] == {"requestedAt": STAMP}
    with pytest.raises(ValueError, match="a list of space ids"):
        mod.set_chat_spaces("s1")


def test_status_names_the_state_apps_and_mailboxes():
    mod, _, cfg, _, _ = kernel(config=on_request())
    assert mod.status()["state"] == "waiting"
    mod.main({})
    out = mod.status()
    assert out["state"] == "ready"
    assert out["apps"] == ["meetings", "people"] and out["mailboxes"] == ["me@example.com"]
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    assert mod.status()["state"] == "switching_off"
