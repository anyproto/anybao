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
                assert op["type"] == "$set" and op["path"] in ("enabled", "program", "args")
                store[r["id"]][op["path"]] = op["value"]
            paths = [op["path"] for op in r["ops"]]
            if paths == ["enabled"]:
                self.calls.append(("enabled", r["id"], r["ops"][0]["value"]))
            else:
                self.calls.append(("rewrite", r["id"], paths))
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

    spaces = ()            # the account's space rows (watch_chats resolves against them)

    def list_spaces(self, raw=False):
        return [dict(r) for r in self.spaces]

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


def switched(fake):
    """The jobs the brain's switch turns on and off: all but the chat scan,
    which follows brain.chatScan.spaces (off while no chat is watched)."""
    return {rid: r for rid, r in jobs(fake).items() if rid != "brain-chat-scan"}


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
    assert sorted(fake.datasets) == ["approvals", "asks", "cards", "config", "drafts",
                                     "facts", "incidents", "observations", "promises",
                                     "sends", "signals", "standups"]
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
    # one tier row per brain job that calls a model (the facts extractor,
    # the reply drafter, the caretaker)
    assert cfg["llm.tier.brain.extractor"] == {"model": "codegen-model"}
    assert cfg["llm.tier.brain.drafter"] == {"model": "codegen-model"}
    assert cfg["llm.tier.brain.caretaker"] == {"model": "codegen-model"}
    assert sorted(k for k in cfg if k.startswith("llm.tier.brain.")) == [
        "llm.tier.brain.caretaker", "llm.tier.brain.drafter", "llm.tier.brain.extractor"]
    ready = statuses(writes)[-1]
    assert ready["hub"] == {"objectId": "hub", "version": 7, "datasets": {
        "signals": "type-hub_signals", "config": "type-hub_config",
        "incidents": "type-hub_incidents", "observations": "type-hub_observations",
        "promises": "type-hub_promises", "facts": "type-hub_facts",
        "cards": "type-hub_cards", "drafts": "type-hub_drafts",
        "approvals": "type-hub_approvals", "standups": "type-hub_standups",
        "sends": "type-hub_sends", "asks": "type-hub_asks"}}
    blocklist = fake.records[(BAO, "hub", "type-hub_config")]["blocklist"]["value"]
    assert blocklist == {"domains": [], "senders": [], "labels": []}


def test_setup_writes_the_jobs_unowned_and_enabled():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    js = jobs(fake)
    assert sorted(js) == ["brain-caretaker", "brain-chat-scan", "brain-facts",
                          "brain-heartbeat", "brain-mail-sync", "brain-people",
                          "brain-promises", "brain-reconcile"]
    facts = js["brain-facts"]
    assert facts["program"] == "connectors:brainFacts@v1" and facts["spec"] == {"every_s": 21600}
    assert facts["args"]["tier"] == "brain.extractor"
    assert facts["args"]["datasets"]["facts"] == "type-hub_facts"
    assert js["brain-people"]["program"] == "connectors:brainPeople@v1"
    assert js["brain-promises"]["spec"] == {"every_s": 1800}
    assert js["brain-mail-sync"]["program"] == "connectors:gmailSync@v1"
    assert js["brain-mail-sync"]["args"] == {"space": BAO}
    beat = js["brain-heartbeat"]
    assert beat["program"] == "connectors:brainHeartbeat@v3" and beat["spec"] == {"every_s": 600}
    assert beat["args"]["space"] == BAO and beat["args"]["hub"] == "hub"
    assert beat["args"]["tier"] == "brain.drafter"
    assert beat["args"]["datasets"]["cards"] == "type-hub_cards"
    assert beat["args"]["datasets"]["config"] == "type-hub_config"
    assert js["brain-reconcile"]["args"] == {"mode": "reconcile"}
    care = js["brain-caretaker"]
    assert care["program"] == "connectors:brainCaretaker@v1" and care["spec"] == {"every_s": 900}
    assert care["args"]["tier"] == "brain.caretaker" and care["args"]["hub"] == "hub"
    assert care["args"]["datasets"]["incidents"] == "type-hub_incidents"
    assert care["args"]["datasets"]["observations"] == "type-hub_observations"
    assert all(j["enabled"] is True and "owner" not in j for j in switched(fake).values())
    # off until a chat is watched (ADR-039 §1)
    scan = js["brain-chat-scan"]
    assert scan["program"] == "connectors:brainChatScan@v1" and scan["spec"] == {"every_s": 1800}
    assert scan["enabled"] is False and "owner" not in scan
    assert scan["args"]["datasets"]["asks"] == "type-hub_asks"


def test_a_hub_before_the_caretaker_upgrades_to_it_through_reconcile():
    # an install set up at hub version 3 has no caretaker job and no tier row
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    triggers(fake).pop("brain-caretaker")
    del cfg["llm.tier.brain.caretaker"]
    old = dict(statuses(writes)[-1])
    old["hub"] = {**old["hub"], "version": 3}
    cfg["brain.status"] = old
    assert mod.main({"mode": "reconcile"}) == {"reconciled": True, "upgrading": True}
    mod.main({"requestedAt": STAMP})                # the re-armed setup run
    assert jobs(fake)["brain-caretaker"]["program"] == "connectors:brainCaretaker@v1"
    assert cfg["llm.tier.brain.caretaker"] == {"model": "codegen-model"}
    assert statuses(writes)[-1]["hub"]["version"] == 7


def test_a_second_run_only_reads():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    before = list(fake.calls)
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "2026-10-25T11:00:00.000Z",
                          "by": "any-ui"}
    mod.main({})
    new = fake.calls[len(before):]
    assert not [c for c in new if c[0] in ("upsert", "enabled", "rewrite")]
    assert [k for k, _ in writes].count("llm.tier.brain.extractor") == 1
    assert [k for k, _ in writes].count("llm.tier.brain.drafter") == 1
    assert [k for k, _ in writes].count("llm.tier.brain.caretaker") == 1


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
    assert sorted(out["disabled"]) == ["brain-caretaker", "brain-facts", "brain-people",
                                    "brain-promises", "brain-reconcile"]
    off = statuses(writes)[-1]
    assert off == {"spaceId": None, "requestedAt": "s-off", "state": "off", "at": off["at"]}
    # mail sync is not a brain job: switching the brain off leaves it running
    assert jobs(fake)["brain-mail-sync"]["enabled"] is True
    assert all(not j["enabled"] for rid, j in jobs(fake).items() if rid != "brain-mail-sync")
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    out = mod.main({})
    assert sorted(out["reenabled"]) == ["brain-caretaker", "brain-facts", "brain-people",
                                    "brain-promises", "brain-reconcile"]
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
    assert all(j["enabled"] for j in switched(fake).values())


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
    assert sum(not j["enabled"] for j in switched(fake).values()) == 1
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "any-ui"}
    mod.main({})
    assert all(j["enabled"] for j in switched(fake).values())


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
    assert jobs(fake)[new_id]["program"] == "connectors:brainHeartbeat@v3"


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


# --- Phase 3: the decision layer (ADR-036) -----------------------------------

def _fields(draft):
    return {f["key"]: f for f in draft["fields"]}


def _keys(draft, flag):
    return {f["key"] for f in draft["fields"] if f.get(flag)}


def test_setup_declares_the_decision_layer_stores_with_their_exact_fields():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    for key in ("cards", "drafts", "approvals", "standups"):
        draft = fake.datasets[key]
        assert draft["shared"] is True and draft["idRule"] == "user"
        assert draft["deleteBy"] == "author"
        stamps = {f["key"]: f["stamp"] for f in draft["fields"] if "stamp" in f}
        assert stamps == {"creator": "creator", "createdAt": "createTime",
                          "modifiedAt": "modifyTime"}
    cards = fake.datasets["cards"]
    assert cards["search"] == {"title": "title", "text": ["reason"], "scope": "cards"}
    assert set(_fields(cards)) - {"creator", "createdAt", "modifiedAt"} == {
        "kind", "title", "reason", "subjectKey", "sourceUri", "signalId", "personIds",
        "briefPersonIds", "status", "snoozedUntil", "answer", "draftId", "approvalId",
        "draftAttempts", "decidedAt"}
    assert _keys(cards, "required") == {"kind", "title", "reason", "subjectKey"}
    assert _keys(cards, "mutableBy") == {"status", "snoozedUntil", "answer", "draftId",
                                         "approvalId", "draftAttempts", "decidedAt"}
    assert _fields(cards)["snoozedUntil"]["kind"] == "datetime"
    assert _fields(cards)["personIds"]["kind"] == "array"
    drafts = fake.datasets["drafts"]
    assert "search" not in drafts
    assert _keys(drafts, "required") == {"cardId", "sender", "senderAddress", "recipients",
                                         "format", "body", "subject",
                                         "recipientProvenance", "model", "tier", "origin"}
    assert set(_fields(drafts)) - {"creator", "createdAt", "modifiedAt"} == (
        _keys(drafts, "required") | {"inReplyTo", "threadId", "supersedes"})
    assert _keys(drafts, "mutableBy") == set()          # an edit is a new draft
    approvals = fake.datasets["approvals"]
    assert _keys(approvals, "required") == {"draftId", "cardId", "sender", "recipients",
                                            "format", "bodyHash", "reason"}
    assert _keys(approvals, "mutableBy") == {"status", "decidedAt", "consumedByRun",
                                             "rejectedReason", "editedTo"}
    standups = fake.datasets["standups"]
    assert _keys(standups, "required") == {"job", "tick", "did", "counts"}
    assert set(_fields(standups)) - {"creator", "createdAt", "modifiedAt"} == {
        "job", "tick", "did", "needs", "counts", "runId"}


def test_setup_seeds_the_drafting_knobs_once_and_never_overwrites_them():
    mod, fake, cfg, _, _ = kernel(config=on_request())
    mod.main({})
    store = fake.records[(BAO, "hub", "type-hub_config")]
    assert store["drafting"]["value"] == {"maxPerRun": 3, "replyWindowDays": 5,
                                          "meetingSoonHours": 24, "quietSenders": []}
    store["drafting"]["value"] = {"maxPerRun": 1, "replyWindowDays": 2,
                                  "meetingSoonHours": 4, "quietSenders": ["^news@"]}
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-again", "by": "chat"}
    mod.main({})
    assert store["drafting"]["value"]["quietSenders"] == ["^news@"]


def test_an_older_heartbeat_is_rewritten_in_place_and_a_pause_holds():
    fake = FakeAny()
    v1 = {"id": "brain-heartbeat", "name": "brain heartbeat", "kind": "cron",
          "spec": {"every_s": 600}, "program": "connectors:brainHeartbeat@v1",
          "args": {"space": BAO, "hub": "hub", "datasets": {"signals": "type-hub_signals"}},
          "enabled": False, "lastRunAt": 123}
    fake.records[TRIGGERS] = {"brain-heartbeat": dict(v1)}
    mod, _, _, _, _ = kernel(config=on_request(), fake=fake)
    out = mod.main({})
    assert out["upgraded"] == ["brain-heartbeat"]
    beats = [j for j in jobs(fake).values() if "Heartbeat" in j["program"]]
    assert len(beats) == 1                              # no second heartbeat
    (beat,) = beats
    assert beat["program"] == "connectors:brainHeartbeat@v3"
    assert beat["args"]["tier"] == "brain.drafter"
    assert beat["args"]["datasets"]["cards"] == "type-hub_cards"
    assert beat["enabled"] is False and beat["lastRunAt"] == 123    # paused stays paused
    assert ("rewrite", "brain-heartbeat", ["program", "args"]) in fake.calls
    assert not [c for c in fake.calls if c[0] == "enabled" and c[1] == "brain-heartbeat"]


def test_an_enabled_older_heartbeat_stays_enabled_and_a_rerun_rewrites_nothing():
    fake = FakeAny()
    fake.records[TRIGGERS] = {"hb-7": {
        "id": "hb-7", "name": "brain heartbeat", "kind": "cron", "spec": {"every_s": 600},
        "program": "connectors:brainHeartbeat@v1", "args": {}, "enabled": True}}
    mod, _, cfg, _, _ = kernel(config=on_request(), fake=fake)
    mod.main({})
    assert jobs(fake)["hb-7"]["enabled"] is True
    assert jobs(fake)["hb-7"]["program"] == "connectors:brainHeartbeat@v3"
    assert "brain-heartbeat" not in jobs(fake)
    before = len(fake.calls)
    mod.main({"mode": "reconcile"})
    assert not [c for c in fake.calls[before:] if c[0] in ("rewrite", "upsert", "enabled")]


def test_reconcile_upgrades_a_version_3_hub():
    # an install from before the decision layer upgrades itself within the hour
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    old = dict(statuses(writes)[-1])
    old["hub"] = {**old["hub"], "version": 3}
    cfg["brain.status"] = old
    assert mod.main({"mode": "reconcile"}) == {"reconciled": True, "upgrading": True}


# --- Phase 4: the reply hand-off (ADR-037) -----------------------------------

def test_setup_declares_the_sends_store_write_once():
    mod, fake, _, _, _ = kernel(config=on_request())
    mod.main({})
    sends = fake.datasets["sends"]
    assert sends["shared"] is True and sends["idRule"] == "user"
    assert sends["deleteBy"] == "author" and "search" not in sends
    fields = {f["key"]: f for f in sends["fields"]}
    assert set(fields) == {"approvalId", "cardId", "threadId", "messageId", "how", "at",
                           "creator", "createdAt"}
    assert _keys(sends, "required") == {"approvalId", "cardId", "threadId", "how", "at"}
    assert _keys(sends, "mutableBy") == set() and fields["at"]["kind"] == "datetime"


def test_a_v2_heartbeat_moves_to_v3_in_place_and_a_pause_holds():
    fake = FakeAny()
    fake.records[TRIGGERS] = {"brain-heartbeat": {
        "id": "brain-heartbeat", "name": "brain heartbeat", "kind": "cron",
        "spec": {"every_s": 600}, "program": "connectors:brainHeartbeat@v2",
        "args": {"space": BAO, "hub": "hub", "datasets": {}, "tier": "brain.drafter"},
        "enabled": False}}
    mod, _, _, _, _ = kernel(config=on_request(), fake=fake)
    assert mod.main({})["upgraded"] == ["brain-heartbeat"]
    (beat,) = [j for j in jobs(fake).values() if "Heartbeat" in j["program"]]
    assert beat["program"] == "connectors:brainHeartbeat@v3" and beat["enabled"] is False
    assert beat["args"]["datasets"]["sends"] == "type-hub_sends"


def test_reconcile_upgrades_a_version_4_hub():
    mod, fake, cfg, writes, _ = kernel(config=on_request())
    mod.main({})
    old = dict(statuses(writes)[-1])
    old["hub"] = {**old["hub"], "version": 4}
    cfg["brain.status"] = old
    assert mod.main({"mode": "reconcile"}) == {"reconciled": True, "upgrading": True}
# --- Phase 6: which chats the brain reads (ADR-039 §1) ------------------------------

SPACES = ({"id": "team", "name": "Team", "status": "active", "ownRole": "writer",
           "spaceType": "any.space"},
          {"id": "dm-bob", "name": "Bob", "status": "active", "ownRole": "writer",
           "spaceType": "any.onetoone"},
          {"id": "news", "name": "News", "status": "active", "ownRole": "reader",
           "spaceType": "any.space"},
          {"id": BAO, "name": "Bao", "status": "active", "ownRole": "owner",
           "spaceType": "any.space"})


def ready_with_spaces():
    fake = FakeAny()
    fake.spaces = SPACES
    mod, fake, cfg, writes, sleeps = kernel(config=on_request(), fake=fake)
    mod.main({})
    return mod, fake, cfg, writes


def test_setup_declares_the_asks_store_and_the_identity_column():
    mod, fake, cfg, _ = ready_with_spaces()
    asks = fake.datasets["asks"]
    assert asks["shared"] is True and asks["search"] == {"text": ["text", "quote"],
                                                         "scope": "asks"}
    required = {f["key"] for f in asks["fields"] if f.get("required")}
    assert required == {"senderIdentity", "spaceId", "chatId", "kind", "text", "quote",
                        "source", "askedAt"}
    mutable = {f["key"] for f in asks["fields"] if f.get("mutableBy") == "author"}
    assert mutable == {"personId", "status", "taskId", "closedAt"}
    person = {p["xKey"]: p for p in fake.collections["brain_person"]["properties"]}
    assert person["identity"]["xFormat"] == {"type": "text"}
    assert mod.status()["chatScan"] == {"spaceIds": [], "pages": False}


def test_watch_chats_writes_the_row_and_the_setup_run_turns_the_scan_on():
    mod, fake, cfg, writes = ready_with_spaces()
    fake.records[TRIGGERS]["brain-setup"] = {"id": "brain-setup", "kind": "once",
                                             "lastRunAt": NOW}    # the once has fired
    out = mod.watch_chats(["team", "Bob"])                    # an id and an exact name
    assert out["spaceIds"] == ["team", "dm-bob"] and out["state"] == "waiting"
    assert out["watching"] == [{"id": "team", "name": "Team", "oneToOne": False},
                               {"id": "dm-bob", "name": "Bob", "oneToOne": True}]
    row = cfg["brain.chatScan.spaces"]
    assert row["spaceIds"] == ["team", "dm-bob"] and row["by"] == "chat"
    armed = triggers(fake)["brain-setup"]
    assert armed["lastRunAt"] is None and armed["args"] == {"requestedAt": STAMP}
    assert jobs(fake)["brain-chat-scan"]["enabled"] is False
    out = mod.main({"requestedAt": STAMP})
    assert out["restarted"] == []                    # the same request: nothing restarts
    assert jobs(fake)["brain-chat-scan"]["enabled"] is True
    assert mod.watch_chats("team")["spaceIds"] == ["team", "dm-bob"]    # no duplicate
    assert mod.status()["chatScan"]["spaceIds"] == ["team", "dm-bob"]


def test_stopping_the_last_chat_turns_the_scan_off():
    mod, fake, cfg, _ = ready_with_spaces()
    mod.watch_chats(["team"])
    mod.main({})
    assert mod.stop_watching_chats(["Team", "not-watched"])["spaceIds"] == []
    mod.main({})
    assert jobs(fake)["brain-chat-scan"]["enabled"] is False
    assert cfg["brain.chatScan.spaces"]["spaceIds"] == []


def test_watch_chats_refuses_and_writes_nothing():
    mod, fake, cfg, _ = ready_with_spaces()
    for wanted, error in ((["news"], "read_only_space"), ([BAO], "bao_space"),
                          (["nowhere"], "unknown_space"), (["team", "news"], "read_only_space")):
        with pytest.raises(Exception) as err:
            mod.watch_chats(wanted)
        assert type(err.value).__name__ == error, wanted
    assert "brain.chatScan.spaces" not in cfg
    mod, _, cfg, _, _ = kernel()
    for call in (lambda: mod.watch_chats(["team"]), lambda: mod.stop_watching_chats(["team"]),
                 lambda: mod.scan_pages(True)):
        with pytest.raises(Exception) as err:
            call()
        assert type(err.value).__name__ == "not_set_up"
    assert not any(k.startswith("brain.chatScan") for k in cfg)


def test_scan_pages_writes_its_row_only():
    mod, fake, cfg, _ = ready_with_spaces()
    row = mod.scan_pages(True)
    assert row["enabled"] is True and row["by"] == "chat"
    assert cfg["brain.chatScan.pages"] == row and mod.status()["chatScan"]["pages"] is True
    assert mod.scan_pages(False)["enabled"] is False


def test_reconcile_turns_off_a_scan_with_nothing_to_read_but_never_on():
    mod, fake, cfg, _ = ready_with_spaces()
    triggers(fake)["brain-chat-scan"]["enabled"] = True      # flipped by hand
    mod.main({"mode": "reconcile"})
    assert jobs(fake)["brain-chat-scan"]["enabled"] is False
    cfg["brain.chatScan.spaces"] = {"spaceIds": ["team"], "requestedAt": "t", "by": "chat"}
    mod.main({"mode": "reconcile"})
    assert jobs(fake)["brain-chat-scan"]["enabled"] is False   # only a setup run enables


def test_switching_the_brain_off_and_on_keeps_the_scan_following_its_row():
    mod, fake, cfg, _ = ready_with_spaces()
    mod.watch_chats(["team"])
    mod.main({})
    cfg["brain.space"] = {"spaceId": None, "requestedAt": "s-off", "by": "chat"}
    assert "brain-chat-scan" in mod.main({})["disabled"]
    cfg["brain.space"] = {"spaceId": BAO, "requestedAt": "s-on", "by": "chat"}
    mod.main({})
    assert jobs(fake)["brain-chat-scan"]["enabled"] is True


def test_a_deleted_chat_scan_is_rewritten_under_a_fresh_id_and_still_follows_the_row():
    mod, fake, cfg, _ = ready_with_spaces()
    fake.delete(*TRIGGERS, "brain-chat-scan")
    out = mod.main({"mode": "reconcile"})
    (new_id,) = out["written"]
    assert new_id.startswith("brain-chat-scan-") and jobs(fake)[new_id]["enabled"] is False


def test_a_chat_scan_rewritten_while_chats_are_watched_starts_on():
    mod, fake, cfg, _ = ready_with_spaces()
    mod.watch_chats(["team"])
    mod.main({})
    fake.delete(*TRIGGERS, "brain-chat-scan")
    (new_id,) = mod.main({"mode": "reconcile"})["written"]
    assert jobs(fake)[new_id]["enabled"] is True


def test_the_cards_store_gains_items_for_the_daily_suggestion_card():
    # the decision layer declares `cards`; Phase 6 adds the pick-list field
    mod, _, _, _, _ = kernel()
    cards = {"key": "cards", "fields": [{"key": "kind", "kind": "string", "required": True}]}
    out = mod._with_card_items(({"key": "asks", "fields": []}, cards))
    assert [f["key"] for f in out[1]["fields"]] == ["kind", "items"]
    assert out[1]["fields"][1] == {"key": "items", "kind": "array"}
    assert mod._with_card_items(out) == out                  # once
    assert cards["fields"] == [{"key": "kind", "kind": "string", "required": True}]
    assert [d["key"] for d in mod.DATASETS][-1] == "asks"
