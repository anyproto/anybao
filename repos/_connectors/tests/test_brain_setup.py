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
from connectorenv import connector_kernel

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
        self.collections = {}
        self.datasets = {}
        self.not_ready = {}
        self.fail = {}
        self.reject = {}
        self.mailboxes = [{"id": "mb1", "any": {"name": "me@example.com"},
                           "mailbox": {"address": "me@example.com"}}]
        self.objects = {}

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
    def setup_app(self, usecase, space=None, show=True):
        self._maybe_fail("setup_app")
        self.calls.append(("setup_app", usecase))
        self.apps.append(usecase)
        return [{"usecase": usecase, "installed": True}]

    def ensure_bundle(self, space, bundle_id, name=None, root_type=None, **_):
        self._maybe_fail("ensure_bundle")
        self.calls.append(("ensure_bundle", bundle_id, name, root_type))
        return {"bundle": {"id": bundle_id, "rootId": "brain-root"}, "installed": True}

    def create_collection(self, space, body):
        self.calls.append(("create_collection", body["xKey"]))
        self.collections[body["xKey"]] = body
        return {"collectionId": f"col-{body['xKey']}", "xKey": body["xKey"], "created": True}

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

    def list_types(self, space):
        # the mailbox type arrives with the first mail sync
        names = ["page", "profile", "task", *self.types] + (["mailbox"] if self.mailboxes else [])
        return [{"id": f"type-{t}", "xKey": t} for t in names]

    def query_objects(self, space, normalize=True, filter=None, limit=None, **_):
        if set(filter or {}) == {"id"}:
            row = self.objects.get(filter["id"])
            return [row] if row else []
        assert filter == {"any.type": "mailbox"}, filter
        if "mailbox" not in {t["xKey"] for t in self.list_types(space)}:
            raise ValueError('type "mailbox" doesn\'t exist')
        return list(self.mailboxes)


def kernel(config=None, fake=None):
    fake = fake or FakeAny()
    cfg = {"llm.tier.codegen": {"model": "codegen-model"},
           "llm.tier.classify": {"model": "classify-model"}}
    cfg.update(config or {})
    writes = []
    sleeps = []

    cfg.setdefault("google.connected", True)   # what googleAuth.status() answers

    def eff(name, payload):
        if name == "time.now":
            return {"epoch": NOW}
        if name == "oauth.status":
            assert payload == {"provider": "google"}
            return {"connected": cfg["google.connected"], "pending": False,
                    "scopes": [], "account": "me@example.com", "expiresAt": None}
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

    app = connector_kernel(effect=eff, any_client=fake)
    return app.use("brainSetup@v1"), fake, cfg, writes, sleeps


def statuses(writes):
    return [v for k, v in writes if k == "brain.status"]


def triggers(fake):
    return fake.records.get(TRIGGERS, {})


def jobs(fake):
    return {rid: r for rid, r in triggers(fake).items() if r.get("kind") == "cron"}


def on_request(stamp=STAMP):
    return {"brain.space": {"spaceId": BAO, "requestedAt": stamp, "by": "any-ui"}}


def test_a_run_with_no_request_sets_nothing_up_and_writes_nothing():
    # only on() / off() write brain.space: nothing starts the brain on its own
    mod, fake, cfg, writes, _ = kernel()
    assert mod.status()["state"] == "off"
    out = mod.main({"requestedAt": "stale-arm"})
    assert out == {"state": "off", "reason": "no request"}
    assert "brain.space" not in cfg and writes == []
    assert jobs(fake) == {} and not any(fake.records.values())
    assert mod.status()["state"] == "off"


def test_on_refuses_while_google_is_not_connected():
    mod, fake, cfg, writes, _ = kernel(config={"google.connected": False})
    with pytest.raises(Exception, match="googleAuth.connect") as err:
        mod.on()
    assert type(err.value).__name__ == "google_not_connected"
    assert "brain.space" not in cfg and writes == [] and triggers(fake) == {}
    cfg["google.connected"] = True
    assert mod.on()["state"] == "waiting"
    assert cfg["brain.space"]["spaceId"] == BAO


def test_setup_installs_the_apps_bundle_hub_and_seeds():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({"requestedAt": STAMP})
    assert [c[1] for c in fake.calls if c[0] == "setup_app"] == ["people", "meetings", "tasks"]
    assert ("ensure_bundle", "brain/v1", "Brain", "page") in fake.calls
    assert fake.types["brain_hub"]["hidden"] is True and fake.types["brain_hub"]["body"] is False
    assert sorted(fake.datasets) == ["config", "facts", "incidents", "observations",
                                     "promises", "signals"]
    facts = fake.datasets["facts"]
    assert facts["shared"] is True and facts["search"]["scope"] == "facts"
    assert "skipHistory" not in facts  # a supersession stays auditable
    facts_mutable = {f["key"] for f in facts["fields"] if f.get("mutableBy") == "author"}
    assert facts_mutable == {"supersedes", "status", "supersededBy", "moreSources",
                             "decidedAt", "confirmedAt"}
    promises = fake.datasets["promises"]
    assert promises["shared"] is True and promises["search"]["scope"] == "promises"
    mutable = {f["key"] for f in promises["fields"] if f.get("mutableBy") == "author"}
    assert mutable == {"personId", "status", "reason", "closedAt", "taskId"}
    assert sorted(fake.collections) == ["brain_person", "brain_task"]
    person_cols = {p["xKey"] for p in fake.collections["brain_person"]["properties"]}
    assert {"emails", "lastContactAt", "mailCount", "aliases"} <= person_cols
    signals = fake.datasets["signals"]
    assert signals["shared"] is True and signals["search"]["scope"] == "signals"
    required = {f["key"] for f in signals["fields"] if f.get("required")}
    assert required == {"tick", "kind", "subject", "decision", "reason"}
    exclusions = fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"]
    assert exclusions == {"version": 1, "personIds": [], "emails": [], "domains": []}
    # one tier row per brain job that calls a model (the facts extractor)
    assert cfg["llm.tier.brain.extractor"] == {"model": "codegen-model"}
    assert not any(k.startswith("llm.tier.brain.") for k in cfg
                   if k != "llm.tier.brain.extractor")
    ready = statuses(writes)[-1]
    assert ready["hub"] == {"objectId": "hub", "version": 3, "datasets": {
        "signals": "type-hub_signals", "config": "type-hub_config",
        "incidents": "type-hub_incidents", "observations": "type-hub_observations",
        "promises": "type-hub_promises", "facts": "type-hub_facts"}}
    blocklist = fake.records[(BAO, "hub", "type-hub_config")]["blocklist"]["value"]
    assert blocklist == {"domains": [], "senders": [], "labels": []}


def test_setup_writes_the_jobs_unowned_and_enabled():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    js = jobs(fake)
    assert sorted(js) == ["brain-facts", "brain-heartbeat", "brain-mail-sync",
                          "brain-people", "brain-promises", "brain-reconcile"]
    facts = js["brain-facts"]
    assert facts["program"] == "connectors:brainFacts@v1" and facts["spec"] == {"every_s": 21600}
    assert facts["args"]["tier"] == "brain.extractor"
    assert facts["args"]["datasets"]["facts"] == "type-hub_facts"
    assert js["brain-people"]["program"] == "connectors:brainPeople@v1"
    assert js["brain-promises"]["spec"] == {"every_s": 1800}
    assert js["brain-mail-sync"]["program"] == "connectors:gmailSync@v1"
    assert js["brain-mail-sync"]["args"] == {"space": BAO}
    beat = js["brain-heartbeat"]
    assert beat["program"] == "connectors:brainHeartbeat@v1" and beat["spec"] == {"every_s": 600}
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
    assert [k for k, _ in writes].count("llm.tier.brain.extractor") == 1


def test_an_edited_tier_is_never_overwritten():
    mine = {"model": "my-own-model"}
    mod, _, cfg, _, _ = kernel(config={**on_request(), "llm.tier.brain.extractor": mine})
    mod.main({})
    assert cfg["llm.tier.brain.extractor"] == mine


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
    assert sorted(out["disabled"]) == ["brain-facts", "brain-people", "brain-promises",
                                    "brain-reconcile"]
    off = statuses(writes)[-1]
    assert off == {"spaceId": None, "requestedAt": "s-off", "state": "off", "at": off["at"]}
    # mail sync is not a brain job: switching the brain off leaves it running
    assert jobs(fake)["brain-mail-sync"]["enabled"] is True
    assert all(not j["enabled"] for rid, j in jobs(fake).items() if rid != "brain-mail-sync")
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    out = mod.main({})
    assert sorted(out["reenabled"]) == ["brain-facts", "brain-people", "brain-promises",
                                    "brain-reconcile"]
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
    assert all(not j["enabled"] for rid, j in jobs(fake).items() if rid != "brain-mail-sync")
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    assert mod.main({})["state"] == "ready"
    assert all(j["enabled"] for j in jobs(fake).values())


def test_an_off_run_cut_short_still_brings_back_what_it_disabled():
    # the list is written before the jobs are disabled, so a run that fails
    # halfway leaves no disabled job unrecorded
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    calls = {"n": 0}
    real_modify = fake.modify

    def modify_then_fail(space, body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection reset")
        return real_modify(space, body)

    fake.modify = modify_then_fail
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    with pytest.raises(Exception, match="connection reset") as err:
        mod.main({})
    assert type(err.value).__name__ == "provision_failed"
    assert cfg["brain.status"]["state"] == "error"      # a failed off run says so
    fake.modify = real_modify
    assert sum(not j["enabled"] for j in jobs(fake).values()) == 1
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    mod.main({})
    assert all(j["enabled"] for j in jobs(fake).values())


def test_on_keeps_remembering_a_job_it_cannot_see_yet():
    # a trigger record not yet synced to this device is re-enabled later,
    # not forgotten
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    cfg["brain.jobs"] = {"offDisabled": ["brain-heartbeat", "brain-elsewhere"]}
    triggers(fake)["brain-heartbeat"]["enabled"] = False
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    out = mod.main({})
    assert out["reenabled"] == ["brain-heartbeat"]
    assert cfg["brain.jobs"] == {"offDisabled": ["brain-elsewhere"]}


def test_a_malformed_jobs_row_re_enables_nothing_and_says_so():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    cfg["brain.jobs"] = {"offDisabled": "brain-heartbeat"}
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    with pytest.raises(Exception, match="brain.jobs"):
        mod.main({})
    assert cfg["brain.status"]["state"] == "error"


def test_a_stopped_job_is_a_warning_on_a_ready_brain_that_keeps_working():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    ready = statuses(writes)[-1]
    triggers(fake)["brain-heartbeat"].update(enabled=False, lastStatus="auto_disabled",
                                              consecutiveFailures=3)
    mod.main({"mode": "reconcile"})
    status = cfg["brain.status"]
    assert status["state"] == "ready" and status["hub"] == ready["hub"]
    assert status["at"] == ready["at"] and status["mailSync"] == ready["mailSync"]
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


def test_a_stopped_mail_sync_is_reported_too():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake)["brain-mail-sync"].update(enabled=False, lastStatus="auto_disabled")
    mod.main({"mode": "reconcile"})
    assert cfg["brain.status"]["stopped"] == [{"id": "brain-mail-sync", "name": "mail sync"}]


def test_try_again_restarts_a_stopped_job_but_not_a_paused_one():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake)["brain-heartbeat"].update(enabled=False, lastStatus="auto_disabled")
    triggers(fake)["brain-mail-sync"].update(enabled=False, lastStatus="auto_disabled")
    triggers(fake)["brain-reconcile"]["enabled"] = False      # the user paused it
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-retry", "by": "any-ui"}
    out = mod.main({})
    assert out["state"] == "ready" and out["restarted"] == ["brain-heartbeat", "brain-mail-sync"]
    assert jobs(fake)["brain-heartbeat"]["enabled"] is True
    assert jobs(fake)["brain-mail-sync"]["enabled"] is True
    assert jobs(fake)["brain-reconcile"]["enabled"] is False
    assert "stopped" not in cfg["brain.status"]


def test_a_setup_run_for_the_same_request_restarts_nothing():
    # a hub upgrade or a chat-scan change re-arms setup on the request it
    # already answered; only a new request (Try again) restarts a stopped job
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
    assert jobs(fake)[new_id]["program"] == "connectors:brainHeartbeat@v1"


def test_a_rejected_write_fails_the_run_instead_of_passing_for_done():
    fake = FakeAny()
    fake.reject[(BAO, "hub", "type-hub_config", "exclusions")] = "value invalid"
    mod, _, _, writes, _ = kernel(config=on_request(), fake=fake)
    with pytest.raises(Exception, match="seeding the exclusions: value invalid"):
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


def test_an_existing_mail_sync_into_baos_space_is_adopted():
    fake = FakeAny()
    fake.records[TRIGGERS] = {
        "mine": {"id": "mine", "kind": "cron", "program": "connectors:gmailSync@v1",
                 "args": {"space": BAO}, "enabled": True},
        "theirs": {"id": "theirs", "kind": "cron", "program": "connectors:gmailSync@v1",
                   "args": {"space": "work-space"}, "enabled": True}}
    mod, _, _, writes, _ = kernel(config=on_request(), fake=fake)
    mod.main({})
    assert statuses(writes)[-1]["mailSync"] == {
        "mode": "existing", "triggerId": "mine",
        "other": [{"triggerId": "theirs", "space": "work-space"}]}
    assert "brain-mail-sync" not in jobs(fake)


def test_a_mail_sync_into_another_space_is_named_and_one_for_bao_written():
    fake = FakeAny()
    fake.records[TRIGGERS] = {
        "theirs": {"id": "theirs", "kind": "cron", "program": "connectors:gmailSync@v1",
                   "args": {"space": "work-space"}, "enabled": True}}
    mod, _, _, writes, _ = kernel(config=on_request(), fake=fake)
    mod.main({})
    verdict = statuses(writes)[-1]["mailSync"]
    assert verdict["mode"] == "created" and verdict["triggerId"] == "brain-mail-sync"
    assert verdict["other"] == [{"triggerId": "theirs", "space": "work-space"}]
    assert triggers(fake)["theirs"]["args"] == {"space": "work-space"}   # left alone


def test_reconcile_upgrades_an_older_hub_by_re_arming_setup():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    old = dict(statuses(writes)[-1])
    old["hub"] = {k: v for k, v in old["hub"].items() if k != "version"}
    cfg["brain.status"] = old
    triggers(fake).pop("brain-setup", None)
    out = mod.main({"mode": "reconcile"})
    assert out == {"reconciled": True, "upgrading": True}
    armed = triggers(fake)["brain-setup"]
    assert armed["lastRunAt"] is None and armed["args"] == {"requestedAt": STAMP}


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
    assert armed["kind"] == "once" and armed["program"] == "connectors:brainSetup@v1"
    assert armed["spec"] == {"at": NOW} and armed["enabled"] is True
    # a fired once keeps its lastRunAt through a merge-$set: the re-arm clears it
    assert armed["lastRunAt"] is None
    assert armed["args"] == {"requestedAt": cfg["brain.space"]["requestedAt"]}
    mod.on()
    assert cfg["brain.space"]["spaceId"] == BAO


def test_on_and_off_rewrite_a_renamed_or_relocated_setup_once_in_place():
    # the once is found by program and kind, not by its id
    mod, fake, cfg, _, _ = kernel()
    mod.on()
    triggers(fake)["brain-setup-x"] = triggers(fake).pop("brain-setup")
    triggers(fake)["brain-setup-x"].update(id="brain-setup-x", lastRunAt=NOW - 5)
    mod.off()
    assert set(triggers(fake)) == {"brain-setup-x"}
    armed = triggers(fake)["brain-setup-x"]
    assert armed["lastRunAt"] is None and armed["args"] == {
        "requestedAt": cfg["brain.space"]["requestedAt"]}


def test_a_setup_once_deleted_from_the_schedule_list_is_rewritten_under_a_fresh_id():
    # seen live: deleting "brain setup" in Scheduled burned its id, and
    # on()/off() failed for good with "record is deleted; the id cannot be reused"
    mod, fake, cfg, _, _ = kernel()
    mod.on()
    assert mod.main({})["state"] == "ready"
    fake.delete(*TRIGGERS, "brain-setup")
    out = mod.off()
    assert out["state"] == "waiting"
    (rid,) = [r for r, rec in triggers(fake).items() if rec.get("kind") == "once"]
    assert rid.startswith("brain-setup-") and triggers(fake)[rid]["lastRunAt"] is None
    assert triggers(fake)[rid]["args"] == {"requestedAt": cfg["brain.space"]["requestedAt"]}
    assert mod.main({})["state"] == "off"
    # the next request re-arms that fresh record, not the burned id
    mod.on()
    assert [r for r, rec in triggers(fake).items() if rec.get("kind") == "once"] == [rid]
    assert mod.main({})["state"] == "ready"


def test_exclude_appends_normalized_and_never_removes():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    mod.exclude(email=" Boss@Example.com ")
    out = mod.exclude(domain="example.org", email="boss@example.com")
    assert out["emails"] == ["boss@example.com"] and out["domains"] == ["example.org"]
    assert out["version"] == 1


def test_excluding_a_person_also_keeps_their_addresses_out():
    # the copy is what keeps them out once their person object is gone
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    fake.objects["ada"] = {"id": "ada", "any": {"type": "profile",
                                                "collections": ["person", "brain_person"]},
                           "person": {"email": "Ada@Lab.org"},
                           "brain_person": {"emails": ["ada@lab.org", "ada@home.net"]}}
    out = mod.exclude(person="ada")
    assert out["personIds"] == ["ada"]
    assert out["emails"] == ["ada@home.net", "ada@lab.org"]
    stored = fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"]
    assert stored == out


def test_excluding_an_unknown_person_writes_nothing():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    before = dict(fake.records[(BAO, "hub", "type-hub_config")]["exclusions"])
    with pytest.raises(Exception) as err:
        mod.exclude(person="nobody")
    assert type(err.value).__name__ == "unknown_person"
    assert fake.records[(BAO, "hub", "type-hub_config")]["exclusions"] == before


def test_exclude_adds_a_person_and_an_address_in_one_call():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    fake.objects["bob"] = {"id": "bob", "any": {"type": "profile", "collections": ["person"]},
                           "person": {"email": "bob@lab.org"}}
    out = mod.exclude(person="bob", email="Eve@Spy.org", domain="@Rival.com")
    assert out["personIds"] == ["bob"]
    assert out["emails"] == ["eve@spy.org", "bob@lab.org"]
    assert out["domains"] == ["rival.com"]


def test_exclude_refuses_an_object_that_is_not_a_person():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    fake.objects["page1"] = {"id": "page1", "any": {"type": "page", "collections": []}}
    before = dict(fake.records[(BAO, "hub", "type-hub_config")]["exclusions"])
    with pytest.raises(Exception) as err:
        mod.exclude(person="page1")
    assert type(err.value).__name__ == "unknown_person"
    assert fake.records[(BAO, "hub", "type-hub_config")]["exclusions"] == before


def test_exclude_never_rewrites_a_malformed_list_into_a_readable_one():
    # the jobs refuse to read mail while the list is malformed; a rewrite
    # that turned "spy.org" into ["s", "p", …] would let them read it again
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    broken = {"version": 1, "personIds": [], "emails": [], "domains": "spy.org"}
    fake.records[(BAO, "hub", "type-hub_config")]["exclusions"] = {"id": "exclusions",
                                                                   "value": broken}
    with pytest.raises(Exception) as err:
        mod.exclude(email="boss@example.com")
    assert type(err.value).__name__ == "exclusions_unreadable"
    assert fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"] == broken


def test_exclude_refuses_before_setup():
    mod, _, _, _, _ = kernel()
    with pytest.raises(Exception) as err:
        mod.exclude(email="a@b.c")
    assert type(err.value).__name__ == "not_set_up"


def test_status_names_the_state_apps_and_mailboxes():
    mod, _, cfg, _, _ = kernel(config=on_request())
    assert mod.status()["state"] == "waiting"
    mod.main({})
    out = mod.status()
    assert out["state"] == "ready"
    assert out["apps"] == ["meetings", "people", "tasks"]
    assert out["mailboxes"] == ["me@example.com"]
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "any-ui"}
    assert mod.status()["state"] == "switching_off"


def test_status_before_the_first_mail_sync_names_no_mailbox():
    fake = FakeAny()
    fake.mailboxes = []                  # no mailbox type: no sync has run
    mod, _, _, _, _ = kernel(config=on_request(), fake=fake)
    mod.main({})
    out = mod.status()
    assert out["state"] == "ready" and out["mailboxes"] == []


def test_allow_and_deny_model_write_the_consent_row_from_chat():
    # ADR-035 §1: only these two tools write brain.model, after a yes
    mod, _, cfg, writes, _ = kernel()
    assert mod.status()["modelAllowed"] is False
    row = mod.allow_model()
    assert row["allowed"] is True and row["by"] == "chat" and row["requestedAt"].endswith("Z")
    assert cfg["brain.model"] == row and mod.status()["modelAllowed"] is True
    assert mod.deny_model()["allowed"] is False
    assert cfg["brain.model"]["allowed"] is False and mod.status()["modelAllowed"] is False
    assert [k for k, _ in writes] == ["brain.model", "brain.model"]


def _ada(fake):
    fake.objects["ada"] = {"id": "ada", "any": {"type": "profile",
                                                "collections": ["person", "brain_person"]},
                           "person": {"email": "ada@x.io"},
                           "brain_person": {"emails": ["ada@x.io", "a.lovelace@x.io"]}}
    return "ada"


def test_include_removes_what_exclude_added_and_nothing_else():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    pid = _ada(fake)
    mod.exclude(person=pid, email="Bob@Y.io", domain="@spam.io")
    mod.exclude(email="keep@z.io")
    out = mod.include(person=pid, email="bob@y.io", domain="spam.io")
    at = int(NOW * 1000)
    assert out == {"version": 1, "personIds": [], "emails": ["keep@z.io"], "domains": [],
                   # when each item was let back in: the jobs skip older mail
                   "includedAt": {"personIds": {pid: at},
                                  "emails": {"ada@x.io": at, "a.lovelace@x.io": at,
                                             "bob@y.io": at},
                                  "domains": {"spam.io": at}}}
    # the list in the hub is what came back
    assert fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"] == out


def test_excluding_again_drops_the_inclusion_stamp():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    mod.exclude(email="bob@y.io", domain="spam.io")
    mod.include(email="bob@y.io", domain="spam.io")
    out = mod.exclude(email="bob@y.io")
    assert out["emails"] == ["bob@y.io"] and out["includedAt"]["emails"] == {}
    assert out["includedAt"]["domains"] == {"spam.io": int(NOW * 1000)}


def test_include_removes_a_person_whose_object_is_gone():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    pid = _ada(fake)
    mod.exclude(person=pid)
    fake.objects.pop(pid)                 # the user deleted the person
    out = mod.include(person=pid, email="ada@x.io")
    assert out["personIds"] == [] and out["emails"] == ["a.lovelace@x.io"]
    assert mod.include(email="a.lovelace@x.io")["emails"] == []


def test_include_refuses_before_setup_and_a_malformed_list():
    mod, fake, _, _, _ = kernel()
    with pytest.raises(Exception, match="brain is off"):
        mod.include(email="a@b.c")
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    fake.records[(BAO, "hub", "type-hub_config")]["exclusions"]["value"] = {"version": 2}
    with pytest.raises(Exception, match="fix the list"):
        mod.include(email="a@b.c")
