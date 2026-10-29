"""programs/brainCaretaker@v1 — the brain watching itself, under the REAL guest
kernel. No model: the run reads no tier of its own and posts nothing; each
grader's positive and negative case; ages count from `uptimeSince`, so a
restart grades nothing overdue before a job's first post-boot run; a paused
mail sync is the user's choice; one incident per (class, subject), one
never-updated observation per tick; the rule table picks each incident's one
repair, the repairs write only their record and read their target from the
incident; the verify / soak ladder widens to the job's own cadence and waits
while an enable_job repair has not had its run; self-heal leaves `verified`
absent; escalation cards only where the hub has `cards`; batched, cooled-down
notices under the `bao` name; the daily report over the last 24 h only. The
mutation checks at the end delete each guard and watch its test fail."""

import time

import connectorenv
import kernelenv
import pytest
from brain_fakes import DATASETS as HUB_DATASETS
from brain_fakes import HUB, FakeSpace
from connectorenv import connector_kernel

BAO = "bao-space"
T0 = 1_800_000_000
DAY = 86400
GRACE = 120
ME = "me@example.com"
CARETAKER = "connectors:brainCaretaker@v1"
SETUP = "connectors:brainSetup@v1"
PROMISES = "connectors:brainPromises@v1"
FACTS = "connectors:brainFacts@v1"
PEOPLE = "connectors:brainPeople@v1"
HEARTBEAT = "connectors:brainHeartbeat@v1"
GMAIL_SYNC = "connectors:gmailSync@v1"
TRIGGERS = ("triggers", "agent_triggers")
RUNS = ("runs", "agent_runs")
TIER = {"provider": "openai-compat", "backend": "anyai",
        "base_url": "http://127.0.0.1:9/v1", "model": "codex/gpt-5.5", "api_key_ref": None}
EXCLUSIONS = {"version": 1, "personIds": [], "emails": [], "domains": []}
REQUESTED = "2026-10-28T08:00:00Z"
CARDS, STANDUPS = "hubtype_cards", "hubtype_standups"
# the default world is a hub without the decision layer's stores;
# with_card_stores() adds them
DATASETS = {k: v for k, v in HUB_DATASETS.items()
            if k not in ("cards", "drafts", "approvals", "standups", "sends")}
SOURCE = (connectorenv.PROGRAMS_DIR / "brainCaretaker@v1" / "program.py").read_text()
FACTS_WINDOW = 2 * 21600 + GRACE      # a 6 h job's verify and soak window
PEOPLE_WINDOW = 2 * 900 + GRACE       # a 15 min job's: wider than the 15 min floor


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class ConfigError(Exception):
    """The broker's type for every config.get failure."""


def brain_job(tid, program, every, **over):
    rec = {"id": tid, "name": tid.replace("-", " "), "kind": "cron",
           "spec": {"every_s": every}, "program": program,
           "args": {"space": BAO, "hub": HUB, "datasets": dict(DATASETS)},
           "enabled": True, "owner": "peer-a", "lastRunAt": T0 - 60,
           "lastStatus": "ok", "consecutiveFailures": 0}
    rec.update(over)
    return rec


def base_triggers():
    return [
        brain_job("brain-heartbeat", HEARTBEAT, 600),
        brain_job("brain-reconcile", SETUP, 3600, args={"mode": "reconcile"}),
        brain_job("brain-people", PEOPLE, 900),
        brain_job("brain-promises", PROMISES, 1800),
        brain_job("brain-facts", FACTS, 21600, args={"space": BAO, "hub": HUB,
                                                     "datasets": dict(DATASETS),
                                                     "tier": "brain.extractor"}),
        # a `tier` an older setup wrote on the caretaker's own job is not a tier in use
        brain_job("brain-caretaker", CARETAKER, 900, lastRunAt=T0 - 900,
                  args={"space": BAO, "hub": HUB, "datasets": dict(DATASETS),
                        "tier": "brain.caretaker"}),
        {"id": "brain-mail-sync", "name": "mail sync", "kind": "cron",
         "spec": {"every_s": 900}, "program": GMAIL_SYNC, "args": {"space": BAO},
         "enabled": True, "owner": "peer-a", "lastRunAt": T0 - 300, "lastStatus": "ok"},
        {"id": "brain-setup", "name": "brain setup", "kind": "once", "spec": {"at": T0 - DAY},
         "program": SETUP, "args": {"requestedAt": REQUESTED}, "enabled": False,
         "owner": "peer-a", "lastRunAt": T0 - DAY, "lastStatus": "ok"},
    ]


def brain_status(datasets=None):
    return {"spaceId": BAO, "requestedAt": REQUESTED, "state": "ready", "at": iso(T0 - DAY),
            "hub": {"objectId": HUB, "datasets": datasets or dict(DATASETS)},
            "mailSync": {"mode": "created", "triggerId": "brain-mail-sync", "other": []}}


class World(FakeSpace):
    """Bao's space with a healthy brain in it: every job ran a minute ago, mail
    synced ten minutes ago, the lists and tiers in place, and the caretaker
    has ticked every 15 min since the device came up a day ago."""

    def __init__(self):
        super().__init__(BAO)
        self.chat = []
        self.active = "peer-a"
        self.seed_lists()
        self.set_config("caretaker", {"lastTickAt": T0 - 900, "uptimeSince": T0 - DAY,
                                      "lastReportDay": time.strftime("%Y-%m-%d", time.gmtime(T0)),
                                      "noticeCooldownMin": 60})
        for rec in base_triggers():
            self.records.setdefault(TRIGGERS, {})[rec["id"]] = rec
        self.add_object("ss1", "sync_state", name="gmail sync state",
                        sync_state={"cursor": "4711", "mailbox_id": "mb1", "skipped_count": 0},
                        modifiedAt={"$date": (T0 - 600) * 1000})

    # any@v1 beyond FakeSpace
    def bundle_child(self, space, bundle_id, seed, type_key=None, **_):
        return {"objectId": {"bao/triggers/v1": "triggers", "bao/runs/v1": "runs"}[seed]}

    def list_devices(self):
        return {"self": "peer-a", "active": {"bao": self.active}, "devices": []}

    def general_chat(self, space):
        return "chat-1"

    def chat_send(self, space, chat_id, body):
        self.chat.append((chat_id, body))
        return {"messageId": f"msg{len(self.chat)}"}

    # the test's handles
    def trigger(self, tid):
        return self.records[TRIGGERS].get(tid)

    def triggers(self):
        return self.records[TRIGGERS]

    def add_run(self, tid, started, status="ok", error=None):
        rid = f"run-{tid}-{started}"
        self.records.setdefault(RUNS, {})[rid] = {
            "id": rid, "runId": rid, "triggerId": tid, "startedAt": float(started),
            "status": status, "errorType": error}

    def incidents(self):
        return sorted(self.store("incidents").values(), key=lambda r: r["id"])

    def incident(self, fault_class):
        (one,) = [i for i in self.store("incidents").values() if i["faultClass"] == fault_class]
        return one

    def observations(self, iid=None):
        rows = self.store("observations").values()
        return sorted((r for r in rows if iid is None or r["incidentId"] == iid),
                      key=lambda r: r["tick"])

    def signals(self):
        return list(self.store("signals").values())

    def cards(self):
        return self.records.get((HUB, CARDS), {})

    def standups(self):
        return self.records.get((HUB, STANDUPS), {})


def base_cfg():
    return {"brain.space": {"spaceId": BAO, "requestedAt": REQUESTED, "by": "chat"},
            "brain.status": brain_status(),
            "llm.tier.brain.extractor": TIER, "llm.tier.brain.drafter": TIER,
            "llm.tier.classify": TIER, "google.connected": True}


def caretaker_args():
    return {"space": BAO, "hub": HUB, "datasets": dict(DATASETS)}


class Rig:
    """One kernel over one world, a clock, and the runtime's bookkeeping: before
    each run every cron job not in `frozen` ran a minute ago and mail synced ten
    minutes ago, unless `fresh=False`. `tick()` is one caretaker cadence on."""

    def __init__(self, world=None, cfg=None, source=None):
        self.world = world or World()
        self.cfg = {**base_cfg(), **(cfg or {})}
        self.clock = {"now": T0}
        self.effects = []
        self.frozen = set()
        if source is None:
            app = connector_kernel(effect=self._eff, any_client=self.world)
        else:
            def module_source(spec):
                if spec.split(":")[-1] == "brainCaretaker@v1":
                    return source
                return connectorenv._agent_repo_source(spec)
            app = kernelenv.load_kernel(effect=self._eff, any_client=self.world,
                                        programs_dir=connectorenv.PROGRAMS_DIR,
                                        module_source=module_source)
        self.mod = app.use("brainCaretaker@v1")

    def _eff(self, name, payload):
        self.effects.append((name, payload.get("key")))
        if name == "time.now":
            return {"epoch": float(self.clock["now"])}
        if name == "config.get":
            key = payload["key"]
            if key in self.cfg:
                return {"value": self.cfg[key]}
            raise ConfigError(f'no config value for "{key}"')
        if name == "oauth.status":
            return {"connected": self.cfg["google.connected"], "pending": False,
                    "scopes": [], "account": ME, "expiresAt": None}
        raise AssertionError(f"unexpected effect {name}")

    def run(self, at=None, fresh=True, args=None):
        if at is not None:
            self.clock["now"] = at
        now = self.clock["now"]
        if fresh:
            for tid, rec in self.world.triggers().items():
                if rec.get("kind") == "cron" and tid not in self.frozen:
                    rec["lastRunAt"] = now - 60
            if "ss1" not in self.frozen:
                self.world.objects["ss1"]["modifiedAt"] = {"$date": (now - 600) * 1000}
        return self.mod.main(args if args is not None else caretaker_args())

    def tick(self):
        return self.run(at=self.clock["now"] + 900)

    def fail_job(self, tid, failures, stopped=False, error="provider_failed"):
        rec = self.world.trigger(tid)
        rec.update(consecutiveFailures=failures, lastStatus="auto_disabled" if stopped else "error")
        if stopped:
            rec["enabled"] = False
        self.world.add_run(tid, self.clock["now"] - 60, status="FAILED", error=error)

    def job_ran(self, tid, at):
        """The runtime's bookkeeping of one good run of a job."""
        self.world.trigger(tid).update(lastRunAt=at, lastStatus="ok", consecutiveFailures=0)


def reruns(rig):
    return [t for t in rig.world.triggers() if t.startswith("brain-rerun-")]


def overdue_people(rig, by=80):
    """The people job (every 900 s) last ran `by` seconds past its overdue limit."""
    rig.frozen.add("brain-people")
    rig.world.trigger("brain-people")["lastRunAt"] = T0 - PEOPLE_WINDOW - by


# --- the args and the snapshot -------------------------------------------------

def test_args_without_the_hub_are_unconfigured():
    rig = Rig()
    with pytest.raises(Exception) as err:
        rig.run(args={"space": BAO})
    assert type(err.value).__name__ == "unconfigured"
    assert rig.world.store("incidents") == {}


def test_unreadable_job_records_fail_the_snapshot():
    rig = Rig()
    rig.world.bundle_child = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "snapshot_failed"


def test_unreadable_open_incidents_fail_the_snapshot():
    rig = Rig()
    real = rig.world.query

    def query(space, oid, ds, **kw):
        if ds == DATASETS["incidents"]:
            raise RuntimeError("down")
        return real(space, oid, ds, **kw)

    rig.world.query = query
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "snapshot_failed"


def test_the_caretaker_has_no_model():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.fail_job("brain-facts", 3, stopped=True)
    overdue_people(rig)
    rig.run()
    assert [e for e in rig.effects if e[0] not in ("time.now", "config.get", "oauth.status")] == []
    assert ("config.get", "llm.tier.brain.caretaker") not in rig.effects
    assert rig.world.incidents()
    for inc in rig.world.incidents():
        assert not {"hypothesis", "confidence", "evidence", "model", "modelTurnAt"} & set(inc)
        assert inc["summary"] and inc["action"] and inc["actionAt"] == {"$date": T0 * 1000}


# --- a healthy brain ----------------------------------------------------------

def test_a_healthy_brain_writes_nothing_but_its_own_state():
    rig = Rig()
    out = rig.run()
    assert out["faults"] == 0 and out["open"] == []
    assert rig.world.store("incidents") == {} and rig.world.store("observations") == {}
    assert rig.world.chat == []
    state = rig.world.config("caretaker")
    assert state["lastTickAt"] == T0 and state["uptimeSince"] == T0 - DAY
    assert state["noticeCooldownMin"] == 60 and "watch" not in state
    # a caretaker with no state yet counts every age from its first tick
    rig = Rig()
    del rig.world.records[(HUB, DATASETS["config"])]["caretaker"]
    rig.run()
    assert rig.world.config("caretaker")["uptimeSince"] == T0


# --- the graders, from snapshots ----------------------------------------------

def snap(rig, **over):
    """A healthy snapshot, as `snapshot()` reads the World, with `over` applied."""
    s = {"now": T0, "space": BAO, "hub": HUB, "datasets": dict(DATASETS),
         "anchor": "triggers", "uptimeSince": T0 - DAY,
         "triggers": [dict(r) for r in base_triggers()],
         "runs": {}, "brainSpace": base_cfg()["brain.space"], "brainStatus": brain_status(),
         "exclusions": {"id": f"{HUB}/exclusions", "value": dict(EXCLUSIONS)},
         "tiers": {"brain.extractor": TIER, "classify": TIER},
         "credential": True, "activeDevice": "peer-a", "cards": None,
         "syncState": {"id": "ss1", "modifiedAt": {"$date": (T0 - 600) * 1000},
                       "sync_state": {"mailbox_id": "mb1", "cursor": "4711"}}}
    s.update(over)
    return s


def with_job(s, tid, **fields):
    for t in s["triggers"]:
        if t["id"] == tid:
            t.update(fields)
    return s


def classes(faults):
    return sorted((f["faultClass"], f["subject"]) for f in faults)


def test_the_graders_find_nothing_in_a_healthy_snapshot():
    mod = Rig().mod
    assert mod.grade(snap(Rig())) == {}


def test_job_overdue():
    mod = Rig().mod
    s = with_job(snap(Rig()), "brain-people", lastRunAt=T0 - 2 * 900 - 121)
    (fault,) = mod.grade_job_overdue(s)
    assert (fault["faultClass"], fault["subject"]) == ("job_overdue", "brain-people")
    assert fault["evidence"]["owner"] == "peer-a" and fault["evidence"]["everyS"] == 900
    assert fault["evidence"]["activeDevice"] == "peer-a" and fault["evidence"]["overdueS"] == 1
    # within 2 × every_s + 120 s: on time
    assert mod.grade_job_overdue(with_job(snap(Rig()), "brain-people",
                                          lastRunAt=T0 - 2 * 900 - 119)) == []
    # a newer synced run or a caretaker rerun counts as a run
    s = with_job(snap(Rig()), "brain-people", lastRunAt=T0 - 5000)
    s["runs"]["brain-people"] = {"startedAt": float(T0 - 100), "status": "ok"}
    assert mod.grade_job_overdue(s) == []
    s = with_job(snap(Rig()), "brain-people", lastRunAt=T0 - 5000)
    s["triggers"].append({"id": "brain-rerun-brain-people-1", "kind": "once",
                          "lastRunAt": T0 - 100})
    assert mod.grade_job_overdue(s) == []


def test_a_paused_job_the_caretaker_itself_and_a_fresh_uptime_are_never_overdue():
    mod = Rig().mod
    paused = with_job(snap(Rig()), "brain-people", lastRunAt=T0 - DAY, enabled=False)
    assert mod.grade_job_overdue(paused) == []
    itself = with_job(snap(Rig()), "brain-caretaker", lastRunAt=T0 - 2000)
    assert mod.grade_job_overdue(with_job(itself, "brain-people", lastRunAt=T0 - 60)) == []
    # the device came up half an hour ago: a stamp from before that says nothing
    up = with_job(snap(Rig(), uptimeSince=T0 - 1800), "brain-people", lastRunAt=T0 - DAY)
    assert mod.grade_job_overdue(up) == []
    up["uptimeSince"] = T0 - PEOPLE_WINDOW - 1
    assert classes(mod.grade_job_overdue(up)) == [("job_overdue", "brain-people")]


def test_a_job_that_never_ran_counts_from_the_setup_answer():
    mod = Rig().mod
    s = with_job(snap(Rig()), "brain-people", lastRunAt=None)
    assert classes(mod.grade_job_overdue(s)) == [("job_overdue", "brain-people")]
    s["brainStatus"] = {**brain_status(), "at": iso(T0 - 600)}
    assert mod.grade_job_overdue(s) == []


def test_job_failing():
    mod = Rig().mod
    s = with_job(snap(Rig()), "brain-promises", consecutiveFailures=2, lastStatus="error")
    s["runs"]["brain-promises"] = {"startedAt": float(T0 - 60), "status": "FAILED",
                                   "errorType": "cursor_corrupt"}
    (fault,) = mod.grade_job_failing(s)
    assert fault["subject"] == "brain-promises"
    assert fault["evidence"] == {"consecutiveFailures": 2, "lastStatus": "error",
                                 "lastErrorType": "cursor_corrupt", "lastRunAt": T0 - 60,
                                 "enabled": True}
    stopped = with_job(snap(Rig()), "brain-facts", lastStatus="auto_disabled",
                       consecutiveFailures=3, enabled=False)
    assert classes(mod.grade_job_failing(stopped)) == [("job_failing", "brain-facts")]
    assert mod.grade_job_failing(with_job(snap(Rig()), "brain-facts",
                                          consecutiveFailures=1)) == []
    # paused after two failures: the user's choice, not the runtime's
    paused = with_job(snap(Rig()), "brain-facts", consecutiveFailures=2, enabled=False)
    assert mod.grade_job_failing(paused) == []
    # re-enabled and not run since: still graded (a pending repair reads it),
    # but marked as awaiting the job's own run
    back = with_job(snap(Rig()), "brain-facts", lastStatus="auto_disabled",
                    consecutiveFailures=3, enabled=True)
    (fault,) = mod.grade_job_failing(back)
    assert mod.awaiting_run(fault) and not mod.awaiting_run(mod.grade_job_failing(stopped)[0])
    # the caretaker does not grade itself: a run reading its own two failures is
    # the run that ends them
    itself = with_job(snap(Rig()), "brain-caretaker", consecutiveFailures=2, lastStatus="error")
    assert mod.grade_job_failing(itself) == []
    # the unread run is passed on as "unavailable", never omitted
    s = with_job(snap(Rig()), "brain-people", consecutiveFailures=2)
    s["runs"]["brain-people"] = "unavailable"
    assert mod.grade_job_failing(s)[0]["evidence"]["lastErrorType"] == "unavailable"


def stale_sync(rig, **over):
    return snap(rig, syncState={"id": "ss1", "modifiedAt": {"$date": (T0 - 6 * 3600 - 1) * 1000},
                                "sync_state": {"mailbox_id": "mb1", "cursor": "4711",
                                               "skipped_count": 2}}, **over)


def test_mail_stale():
    mod = Rig().mod
    (fault,) = mod.grade_mail_stale(snap(Rig(), credential=False))
    assert fault["subject"] == "mb1" and fault["evidence"]["credentialConnected"] is False
    assert fault["evidence"]["mailTriggerId"] == "brain-mail-sync"
    (fault,) = mod.grade_mail_stale(stale_sync(Rig()))
    assert fault["evidence"]["syncStateModifiedAt"] == T0 - 6 * 3600 - 1
    assert fault["evidence"]["cursor"] == "4711" and fault["evidence"]["skippedCount"] == 2
    assert mod.grade_mail_stale(snap(Rig())) == []
    assert mod.grade_mail_stale(snap(Rig(), credential="unavailable")) == []
    # a fresh uptime suppresses the age, not a dead credential
    up = stale_sync(Rig(), uptimeSince=T0 - 3600)
    assert mod.grade_mail_stale(up) == []
    up["credential"] = False
    assert len(mod.grade_mail_stale(up)) == 1


def test_a_paused_mail_sync_is_not_stale():
    mod = Rig().mod
    paused = with_job(stale_sync(Rig(), credential=False), "brain-mail-sync", enabled=False)
    assert mod.grade_mail_stale(paused) == []
    # stopped by the runtime is not a pause
    stopped = with_job(stale_sync(Rig()), "brain-mail-sync", enabled=False,
                       lastStatus="auto_disabled")
    assert classes(mod.grade_mail_stale(stopped)) == [("mail_stale", "mb1")]
    # and live: a paused sync is never rerun
    rig = Rig(cfg={"google.connected": False})
    rig.world.trigger("brain-mail-sync")["enabled"] = False
    out = rig.run()
    assert out["faults"] == 0 and reruns(rig) == [] and rig.world.chat == []


def test_exclusions_unreadable():
    mod = Rig().mod
    (fault,) = mod.grade_exclusions_unreadable(snap(Rig(), exclusions=None))
    assert fault["subject"] == "config/exclusions"
    assert fault["evidence"] == {"present": False, "version": None,
                                 "parseError": "no exclusion list"}
    v2 = snap(Rig(), exclusions={"id": "hub/exclusions", "value": {**EXCLUSIONS, "version": 2}})
    assert mod.grade_exclusions_unreadable(v2)[0]["evidence"]["version"] == 2
    assert mod.grade_exclusions_unreadable(snap(Rig())) == []
    assert mod.grade_exclusions_unreadable(snap(Rig(), exclusions="unavailable")) == []


def test_tier_unpinned_covers_the_tiers_the_jobs_call():
    rig = Rig()
    del rig.cfg["llm.tier.classify"]                    # the promises job's tier
    rig.cfg["llm.tier.brain.extractor"] = {"provider": "openai-compat"}   # no model
    rig.run()
    assert sorted(i["subject"] for i in rig.world.incidents()) == [
        "llm.tier.brain.extractor", "llm.tier.classify"]
    ev = {i["subject"]: rig.world.observations(i["id"])[0]["evidence"]
          for i in rig.world.incidents()}
    assert ev["llm.tier.classify"] == {"present": False, "provider": None, "model": None}
    assert ev["llm.tier.brain.extractor"]["provider"] == "openai-compat"
    # the drafter's tier is checked once the hub has drafts; the caretaker's own
    # job calls no model, whatever its args say
    mod = Rig().mod
    s = snap(Rig(), datasets={**DATASETS, "drafts": "hubtype_drafts"})
    s["tiers"]["brain.drafter"] = None
    assert classes(mod.grade_tier_unpinned(s)) == [("tier_unpinned", "llm.tier.brain.drafter")]
    assert mod._tiers_in_use(s["triggers"], s["datasets"]) == [
        "brain.drafter", "brain.extractor", "classify"]


def test_setup_blocked():
    mod = Rig().mod
    waiting = {"spaceId": BAO, "requestedAt": iso(T0 - 3601), "by": "chat"}
    (fault,) = mod.grade_setup_blocked(snap(Rig(), brainSpace=waiting))
    assert fault["subject"] == "brain-setup" and fault["evidence"]["requestState"] == "waiting"
    assert fault["evidence"]["ageS"] == 3601 and fault["evidence"]["setupOwner"] == "peer-a"
    fresh = {**waiting, "requestedAt": iso(T0 - 1800)}
    assert mod.grade_setup_blocked(snap(Rig(), brainSpace=fresh)) == []
    # the request predates a boot half an hour ago: the age counts from the boot
    assert mod.grade_setup_blocked(snap(Rig(), brainSpace=waiting, uptimeSince=T0 - 1800)) == []
    provisioning = {**brain_status(), "requestedAt": iso(T0 - 9000), "state": "provisioning",
                    "at": iso(T0 - 3700)}
    s = snap(Rig(), brainSpace={**waiting, "requestedAt": iso(T0 - 9000)},
             brainStatus=provisioning)
    assert mod.grade_setup_blocked(s)[0]["evidence"]["requestState"] == "setting_up"
    gone = snap(Rig())
    gone["triggers"] = [t for t in gone["triggers"] if t["id"] != "brain-setup"]
    (fault,) = mod.grade_setup_blocked(gone)
    assert fault["evidence"]["setupTriggerPresent"] is False
    assert mod.grade_setup_blocked(snap(Rig())) == []
    # setup rewrites a deleted brain-setup under a fresh id: found by program and kind
    moved = snap(Rig())
    moved["triggers"] = [{**t, "id": "brain-setup-1793000000"} if t["id"] == "brain-setup" else t
                         for t in moved["triggers"]]
    assert mod.grade_setup_blocked(moved) == []


def test_draft_backlog_once_the_hub_has_cards():
    mod = Rig().mod

    def reply(i, age, **over):
        return {"id": f"hub/c{i}", "kind": "reply", "status": "open",
                "createdAt": {"$date": (T0 - age) * 1000}, **over}

    assert mod.grade_draft_backlog(snap(Rig(), cards=None)) == []
    three = [reply(i, 4000) for i in range(3)]
    (fault,) = mod.grade_draft_backlog(snap(Rig(), cards=three))
    assert fault["subject"] == "cards" and fault["evidence"]["count"] == 3
    assert fault["evidence"]["oldestOpenAt"] == T0 - 4000
    assert mod.grade_draft_backlog(snap(Rig(), cards=[*three[:2], reply(9, 100)])) == []
    drafted = [reply(i, 4000, draftId="d") for i in range(3)]
    assert mod.grade_draft_backlog(snap(Rig(), cards=drafted)) == []
    spent = [reply(1, 100, draftAttempts=3)]
    assert mod.grade_draft_backlog(snap(Rig(), cards=spent))[0]["evidence"][
        "attemptsExhausted"] == 1


# --- the age base: uptimeSince -------------------------------------------------

def test_uptime_since_follows_the_caretakers_own_ticks_and_cadence():
    mod = Rig().mod
    triggers = base_triggers()
    assert mod.uptime_since({}, triggers, T0) == T0                      # never ticked
    assert mod.uptime_since({"lastTickAt": T0 - 900}, triggers, T0) == T0   # no base yet
    held = {"lastTickAt": T0 - 900, "uptimeSince": T0 - DAY}
    assert mod.uptime_since(held, triggers, T0) == T0 - DAY              # a tick on time
    assert mod.uptime_since({**held, "lastTickAt": T0 - 1020}, triggers, T0) == T0 - DAY
    assert mod.uptime_since({**held, "lastTickAt": T0 - 1021}, triggers, T0) == T0   # a gap
    # the gap is measured against the record's own cadence, not the constant
    hourly = [{**t, "spec": {"every_s": 3600}} if t["id"] == "brain-caretaker" else t
              for t in triggers]
    assert mod.uptime_since({**held, "lastTickAt": T0 - 3000}, hourly, T0) == T0 - DAY
    assert mod.own_every_s(hourly) == 3600 and mod.own_every_s([]) == 900


def test_a_restart_grades_nothing_overdue_before_the_jobs_first_run():
    """The facts job (6 h) last ran 5 h before the device went down for an hour.
    The runtime re-arms it at boot + 6 h; the caretaker waits two cadences and
    the grace from the boot before it calls the job overdue."""
    rig = Rig()
    rig.frozen.add("brain-facts")
    rig.world.trigger("brain-facts")["lastRunAt"] = T0 - 5 * 3600
    rig.run(at=T0)
    assert rig.world.config("caretaker")["uptimeSince"] == T0 - DAY
    boot = T0 + 3600
    first = None
    for n in range(0, 60):
        out = rig.run(at=boot + n * 900)
        if n == 0:
            assert rig.world.config("caretaker")["uptimeSince"] == boot
        if out["faults"] and first is None:
            first = boot + n * 900
    assert first == boot + 49 * 900           # the first tick past boot + 12 h 2 min
    inc = rig.world.incident("job_overdue")
    assert inc["subject"] == "brain-facts" and inc["openedAt"] == {"$date": first * 1000}
    assert rig.world.config("caretaker")["uptimeSince"] == boot


# --- incidents and observations -------------------------------------------------

def test_one_incident_per_fault_and_one_never_updated_observation_per_tick():
    rig = Rig()
    overdue_people(rig)
    rig.run(at=T0)
    rig.tick()
    (inc,) = rig.world.incidents()
    assert inc["faultClass"] == "job_overdue" and inc["subject"] == "brain-people"
    assert inc["openedAt"] == {"$date": T0 * 1000}
    assert inc["id"] == rig.mod.incident_id("job_overdue", "brain-people", T0)
    assert inc["summary"] == "brain people is overdue by 1 h"
    obs = rig.world.observations(inc["id"])
    assert [o["id"] for o in obs] == [f"{inc['id']}:{T0}", f"{inc['id']}:{T0 + 900}"]
    assert obs[0]["evidence"]["overdueS"] == 80
    # observations go through upsert_records only: never a modify
    assert not [c for c in rig.world.calls if c[0] == "modify" and c[1] == "observations"]
    assert [c for c in rig.world.calls if c[0] == "upsert_records" and c[1] == "observations"]


def test_a_fault_that_clears_by_itself_closes_with_verified_absent():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)
    assert rig.world.incident("job_failing")["status"] == "escalated"
    rig.job_ran("brain-people", T0 + 800)
    rig.tick()
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "closed" and inc["closedAt"] == {"$date": (T0 + 900) * 1000}
    assert "verified" not in inc
    assert rig.world.observations(inc["id"])[-1]["evidence"] == {"clean": True}
    rig.tick()                                         # closed: no more observations
    assert len(rig.world.observations(inc["id"])) == 2


def test_an_escalated_fault_waiting_on_the_user_is_observed_hourly():
    rig = Rig()
    del rig.world.triggers()["brain-setup"]
    for tick in range(T0, T0 + 4501, 900):
        rig.run(at=tick)
    inc = rig.world.incident("setup_blocked")
    assert [o["tick"] for o in rig.world.observations(inc["id"])] == [T0, T0 + 900, T0 + 4500]


# --- the rule table -------------------------------------------------------------

def test_the_rule_table():
    mod = Rig().mod
    s = snap(Rig())
    assert mod.repair_for("job_overdue", "brain-people", s) == "run_job"
    assert mod.repair_for("job_overdue", "brain-mail-sync", s) == "escalate"   # not a brain job
    stopped = with_job(snap(Rig()), "brain-facts", lastStatus="auto_disabled", enabled=False)
    assert mod.repair_for("job_failing", "brain-facts", stopped) == "enable_job"
    assert mod.repair_for("job_failing", "brain-people", stopped) == "escalate"  # failing, running
    assert mod.repair_for("job_failing", "brain-mail-sync", stopped) == "escalate"
    assert mod.repair_for("mail_stale", "mb1", s) == "run_job"
    stopped_sync = with_job(snap(Rig()), "brain-mail-sync", enabled=False,
                            lastStatus="auto_disabled")
    assert mod.repair_for("mail_stale", "mb1", stopped_sync) == "escalate"
    unnamed = snap(Rig(), brainStatus={**brain_status(), "mailSync": None})
    assert mod.repair_for("mail_stale", "mail", unnamed) == "escalate"
    for cls, subject in (("setup_blocked", "brain-setup"), ("tier_unpinned", "llm.tier.classify"),
                         ("exclusions_unreadable", "config/exclusions"),
                         ("draft_backlog", "cards")):
        assert mod.repair_for(cls, subject, s) == "escalate"


def test_windows_and_cadence():
    mod = Rig().mod
    assert mod.windows(None) == (900, 3600)
    assert mod.windows(600) == (1320, 3600)
    assert mod.windows(900) == (1920, 3600)
    assert mod.windows(21600) == (FACTS_WINDOW, FACTS_WINDOW)
    s = snap(Rig())
    assert mod.cadence_s("job_failing", "brain-facts", s) == 21600
    assert mod.cadence_s("mail_stale", "mb1", s) == 900
    assert mod.cadence_s("tier_unpinned", "llm.tier.classify", s) is None


# --- the repairs ----------------------------------------------------------------

def test_run_job_arms_a_once_trigger_with_the_jobs_program_and_args():
    rig = Rig()
    overdue_people(rig)
    rig.run()
    (rid,) = reruns(rig)
    assert rid == f"brain-rerun-brain-people-{T0}"
    rerun, job = rig.world.trigger(rid), rig.world.trigger("brain-people")
    assert rerun["kind"] == "once" and rerun["spec"] == {"at": T0} and rerun["enabled"] is True
    assert rerun["program"] == PEOPLE and rerun["args"] == job["args"]
    assert "lastRunAt" not in rerun and "owner" not in rerun
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "repairing" and inc["action"] == "run_job"
    assert inc["actionAt"] == {"$date": T0 * 1000}
    assert inc["verifyAt"] == {"$date": (T0 + PEOPLE_WINDOW) * 1000}   # two cadences, not 15 min
    assert rig.world.chat == [] and rig.world.signals() == []


def test_a_taken_rerun_id_gets_a_fresh_one():
    rig = Rig()
    rig.world.triggers()[f"brain-rerun-brain-people-{T0}"] = {
        "id": f"brain-rerun-brain-people-{T0}", "kind": "once", "program": PEOPLE}
    overdue_people(rig)
    rig.run()
    assert f"brain-rerun-brain-people-{T0}-2" in rig.world.triggers()


def test_enable_job_writes_enabled_true_on_the_jobs_record_and_nothing_else():
    rig = Rig()
    rig.fail_job("brain-facts", 3, stopped=True)
    before = {k: dict(v) for k, v in rig.world.triggers().items()}
    rig.run()
    modifies = [c for c in rig.world.calls if c[0] == "modify" and c[1] == "agent_triggers"]
    assert modifies == [("modify", "agent_triggers", "brain-facts", ["enabled"])]
    after = rig.world.triggers()
    assert after["brain-facts"] == {**before["brain-facts"], "enabled": True, "lastRunAt": T0 - 60}
    assert set(after) == set(before) and reruns(rig) == []
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "repairing" and inc["action"] == "enable_job"
    assert inc["verifyAt"] == {"$date": (T0 + FACTS_WINDOW) * 1000}


def test_a_failing_job_that_still_runs_is_escalated_not_repaired():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.run()
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    assert inc["actionAt"] == {"$date": T0 * 1000} and "verifyAt" not in inc
    assert reruns(rig) == [] and rig.world.trigger("brain-people")["enabled"] is True
    (_, body), = rig.world.chat
    assert "brain people failed 2 times in a row" in body["text"]


def test_mail_stale_reruns_an_enabled_mail_sync():
    rig = Rig(cfg={"google.connected": False})
    rig.run()
    inc = rig.world.incident("mail_stale")
    assert inc["subject"] == "mb1" and inc["action"] == "run_job"
    assert inc["verifyAt"] == {"$date": (T0 + PEOPLE_WINDOW) * 1000}   # the sync's cadence
    (rid,) = reruns(rig)
    assert rid == f"brain-rerun-brain-mail-sync-{T0}"
    assert rig.world.trigger(rid)["program"] == GMAIL_SYNC
    assert rig.world.trigger(rid)["args"] == {"space": BAO}


def test_a_mail_sync_the_runtime_stopped_is_escalated_not_rerun():
    rig = Rig(cfg={"google.connected": False})
    rig.world.trigger("brain-mail-sync").update(enabled=False, lastStatus="auto_disabled")
    rig.run()
    inc = rig.world.incident("mail_stale")
    assert inc["status"] == "escalated" and reruns(rig) == []
    assert len(rig.world.chat) == 1


def test_a_repair_that_cannot_be_written_is_a_refused_signal_and_an_escalation():
    rig = Rig()
    rig.fail_job("brain-facts", 3, stopped=True)
    real = rig.world.modify

    def modify(space, body):
        if body["dataset"] == "agent_triggers":
            raise RuntimeError("read-only replica")
        return real(space, body)

    rig.world.modify = modify
    rig.run()
    (sig,) = rig.world.signals()
    inc = rig.world.incident("job_failing")
    assert sig["kind"] == "repair_refused" and sig["decision"] == "refused"
    assert sig["subject"] == inc["id"] and sig["tick"] == T0
    assert "enable_job failed" in sig["reason"] and "read-only replica" in sig["reason"]
    assert inc["status"] == "escalated" and inc["action"] == "enable_job"   # what it tried
    assert rig.world.trigger("brain-facts")["enabled"] is False
    (_, body), = rig.world.chat
    assert "read-only replica" in body["text"]
    rig.tick()                                         # and it is not tried again
    assert len(rig.world.signals()) == 1


def test_a_job_the_runtime_stops_after_its_notice_still_gets_its_enable_job():
    rig = Rig()
    rig.frozen.add("brain-people")
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)                                     # failing, still running: a notice
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    rig.fail_job("brain-people", 3, stopped=True)      # the third failure stops it
    rig.tick()
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "repairing" and inc["action"] == "enable_job"
    assert inc["actionAt"] == {"$date": (T0 + 900) * 1000}
    assert rig.world.trigger("brain-people")["enabled"] is True
    assert len(rig.world.incidents()) == 1 and len(rig.world.chat) == 1


# --- the ladder -------------------------------------------------------------------

def test_a_repair_is_verified_then_soaked_before_it_is_fixed():
    rig = Rig()
    overdue_people(rig)
    rig.run(at=T0)
    (rid,) = reruns(rig)
    rig.world.trigger(rid).update(lastRunAt=T0 + 100, lastStatus="ok")   # the rerun ran
    rig.frozen.discard("brain-people")                 # and the job keeps running on its own
    rig.tick()                                         # clean, but before verifyAt
    assert rig.world.incident("job_overdue")["status"] == "repairing"
    rig.run(at=T0 + PEOPLE_WINDOW)
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "soaking"
    assert inc["verifyAt"] == {"$date": (T0 + PEOPLE_WINDOW + 3600) * 1000}
    rig.tick()
    assert rig.world.incident("job_overdue")["status"] == "soaking"
    rig.run(at=T0 + PEOPLE_WINDOW + 3600)
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "closed" and inc["verified"] is True
    assert inc["closedAt"] == {"$date": (T0 + PEOPLE_WINDOW + 3600) * 1000}
    assert rig.world.chat == [] and len(reruns(rig)) == 1   # success is silent


def test_a_repair_that_does_not_hold_escalates_once_with_no_second_repair():
    rig = Rig()
    overdue_people(rig)
    rig.run(at=T0)
    rig.tick()                                         # still overdue, before verifyAt
    assert rig.world.incident("job_overdue")["status"] == "repairing" and rig.world.chat == []
    rig.run(at=T0 + PEOPLE_WINDOW)                     # the rerun never ran: dirty at verifyAt
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "escalated" and inc["verified"] is False
    assert inc["action"] == "run_job"                  # what it tried stays on the record
    (_, body), = rig.world.chat
    assert "(the repair did not hold)" in body["text"]
    for _ in range(8):
        rig.tick()
    assert len(reruns(rig)) == 1 and len(rig.world.chat) == 1
    rig.frozen.discard("brain-people")
    rig.tick()                                         # then it clears: closed, still unverified
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "closed" and inc["verified"] is False


def test_a_dirty_tick_during_the_soak_escalates():
    rig = Rig()
    overdue_people(rig)
    rig.run(at=T0)
    rig.frozen.discard("brain-people")
    for _ in range(3):
        rig.tick()                                     # T0 + 45 min: past verifyAt, clean
    assert rig.world.incident("job_overdue")["status"] == "soaking"
    rig.frozen.add("brain-people")
    rig.world.trigger("brain-people")["lastRunAt"] = T0 - DAY
    rig.tick()
    inc = rig.world.incident("job_overdue")
    assert inc["status"] == "escalated" and inc["verified"] is False


def test_enable_job_verifies_after_the_jobs_own_run_not_at_fifteen_minutes():
    """A 6 h job the runtime stopped keeps `auto_disabled` on its record until its
    next own run. The repair is pending until then: no escalation at +15 min."""
    rig = Rig()
    rig.frozen.add("brain-facts")
    rig.fail_job("brain-facts", 3, stopped=True)
    rig.run(at=T0)
    iid = rig.world.incident("job_failing")["id"]
    rig.tick()                                         # +15 min: enabled, still auto_disabled
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "repairing" and rig.world.chat == []
    obs = rig.world.observations(iid)[-1]["evidence"]
    assert obs["pending"] is True and obs["lastStatus"] == "auto_disabled" and obs["enabled"]
    for _ in range(4):
        rig.tick()
    assert rig.world.incident("job_failing")["status"] == "repairing"
    rig.job_ran("brain-facts", T0 + 21600)             # its own next run, six hours on
    rig.run(at=T0 + 21600 + 300)                       # clean, before verifyAt
    assert rig.world.incident("job_failing")["status"] == "repairing"
    assert rig.world.observations(iid)[-1]["evidence"] == {"clean": True}
    rig.run(at=T0 + FACTS_WINDOW)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "soaking"
    assert inc["verifyAt"] == {"$date": (T0 + 2 * FACTS_WINDOW) * 1000}
    rig.run(at=T0 + 2 * FACTS_WINDOW)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "closed" and inc["verified"] is True
    assert rig.world.chat == [] and rig.world.signals() == []


def test_a_re_enabled_job_that_fails_again_escalates():
    rig = Rig()
    rig.frozen.add("brain-facts")
    rig.fail_job("brain-facts", 3, stopped=True)
    rig.run(at=T0)
    rig.tick()
    # it ran, failed, and the runtime stopped it again: no longer pending, dirty
    rig.world.trigger("brain-facts").update(lastRunAt=T0 + 21600, enabled=False)
    rig.run(at=T0 + 21600 + 300)
    assert rig.world.observations()[-1]["evidence"].get("pending") is None
    assert rig.world.incident("job_failing")["status"] == "repairing"   # verifyAt not reached
    rig.run(at=T0 + FACTS_WINDOW)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["verified"] is False
    assert inc["action"] == "enable_job" and len(rig.world.chat) == 1


def test_pending_rule():
    mod = Rig().mod
    inc = {"action": "enable_job", "actionAt": {"$date": T0 * 1000}}
    waiting = {"evidence": {"enabled": True, "lastStatus": "auto_disabled", "lastRunAt": T0 - 60}}
    assert mod.pending(inc, waiting) is True
    assert mod.pending(inc, {"evidence": {**waiting["evidence"], "lastRunAt": None}}) is True
    ran = {"evidence": {**waiting["evidence"], "lastRunAt": T0 + 60}}
    assert mod.pending(inc, ran) is False                     # a newer run: the record speaks
    assert mod.pending(inc, {"evidence": {**waiting["evidence"], "enabled": False}}) is False
    assert mod.pending({**inc, "action": "run_job"}, waiting) is False
    assert mod.pending(inc, None) is False


def test_the_ladder_steps():
    mod = Rig().mod
    v = {"$date": T0 * 1000}
    assert mod.step({"status": "open"}, T0, True) == {"status": "closed", "closedAt": T0}
    assert mod.step({"status": "open"}, T0, False) is None
    assert mod.step({"status": "escalated"}, T0, True)["status"] == "closed"
    assert mod.step({"status": "repairing", "verifyAt": v}, T0 - 1, True) is None
    assert mod.step({"status": "repairing", "verifyAt": v}, T0 - 1, False) is None
    assert mod.step({"status": "repairing", "verifyAt": v}, T0, True) == {
        "status": "soaking", "verifyAt": T0 + 3600}
    assert mod.step({"status": "repairing", "verifyAt": v}, T0, True, 7200) == {
        "status": "soaking", "verifyAt": T0 + 7200}
    assert mod.step({"status": "repairing", "verifyAt": v}, T0, False) == {
        "status": "escalated", "verified": False}
    assert mod.step({"status": "repairing", "verifyAt": v}, T0, None) is None   # pending
    assert mod.step({"status": "soaking", "verifyAt": v}, T0 - 1, True) is None
    assert mod.step({"status": "soaking", "verifyAt": v}, T0 - 1, False)["verified"] is False
    assert mod.step({"status": "soaking", "verifyAt": v}, T0, None) is None
    assert mod.step({"status": "soaking", "verifyAt": v}, T0, True)["verified"] is True
    assert mod.step({"status": "closed"}, T0, True) is None


# --- notices ---------------------------------------------------------------------

def test_a_notice_is_one_batched_message_under_the_bao_name_per_cooldown():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.fail_job("brain-promises", 2)
    rig.run(at=T0)
    (chat_id, body), = rig.world.chat
    assert chat_id == "chat-1" and body["agent"] == {"name": "bao", "done": True}
    assert body["text"].startswith("The brain needs a look:\n")
    assert body["text"].count("\n• ") == 2 and "brain people failed 2 times" in body["text"]
    # a third incident inside the cooldown waits; after it, one more message
    del rig.cfg["llm.tier.classify"]
    rig.tick()
    assert len(rig.world.chat) == 1 and rig.world.config("caretaker")["pending"]
    rig.run(at=T0 + 3600)
    assert len(rig.world.chat) == 2 and rig.world.chat[1][1]["agent"]["name"] == "bao"
    assert "no model is set for llm.tier.classify" in rig.world.chat[1][1]["text"]
    assert rig.world.config("caretaker")["pending"] == []


def test_a_notice_that_cannot_be_sent_is_counted_and_kept():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    real = rig.world.chat_send
    rig.world.chat_send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("chat down"))
    out = rig.run(at=T0)
    assert out["noticeFailed"] == 1 and out["notices"] == 0
    assert rig.world.incident("job_failing")["status"] == "escalated"
    state = rig.world.config("caretaker")
    assert len(state["pending"]) == 1 and "lastNoticeAt" not in state
    rig.world.chat_send = real
    out = rig.tick()
    assert out["notices"] == 1 and len(rig.world.chat) == 1
    assert rig.world.config("caretaker")["lastNoticeAt"] == T0 + 900


def test_a_queued_notice_whose_incident_closed_is_dropped():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)                                     # escalated, noticed at once
    assert len(rig.world.chat) == 1
    del rig.world.records[(HUB, DATASETS["config"])]["exclusions"]
    rig.tick()                                         # escalated inside the cooldown
    assert len(rig.world.config("caretaker")["pending"]) == 1
    rig.world.seed_lists()
    rig.tick()                                         # it clears before the notice goes
    assert rig.world.incident("exclusions_unreadable")["status"] == "closed"
    rig.run(at=T0 + 3700)
    assert len(rig.world.chat) == 1 and rig.world.config("caretaker")["pending"] == []


# --- escalation, cards, the report -------------------------------------------------

def with_card_stores(rig):
    datasets = {**DATASETS, "cards": CARDS, "standups": STANDUPS}
    rig.cfg["brain.status"] = brain_status(datasets)


def test_setup_blocked_escalates_with_a_card_and_a_notice():
    rig = Rig()
    with_card_stores(rig)
    del rig.world.triggers()["brain-setup"]
    rig.run()
    inc = rig.world.incident("setup_blocked")
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    assert inc["actionAt"] == {"$date": T0 * 1000}
    (card,) = [c for c in rig.world.cards().values() if c["kind"] == "incident"]
    assert card["kind"] == "incident" and card["subjectKey"] == inc["id"]
    assert card["title"] == "setup_blocked: brain-setup" and card["status"] == "open"
    assert card["id"] == rig.mod.card_id("incident", inc["id"]) == inc["cardId"]
    assert card["reason"] == inc["summary"]
    assert "brain-setup job is gone; switching the brain on again" in card["reason"]
    assert card["sourceUri"] == f"any://o/{BAO}/{HUB}/incidents/{inc['id']}"
    (_, body), = rig.world.chat
    assert "brain-setup job is gone" in body["text"]


def test_without_cards_the_notice_is_all():
    rig = Rig()
    del rig.world.triggers()["brain-setup"]
    rig.run()
    inc = rig.world.incident("setup_blocked")
    assert inc["status"] == "escalated" and "cardId" not in inc
    assert rig.world.cards() == {} and len(rig.world.chat) == 1


def test_a_dismissed_card_stays_dismissed():
    rig = Rig()
    with_card_stores(rig)
    del rig.world.triggers()["brain-setup"]
    iid = rig.mod.incident_id("setup_blocked", "brain-setup", T0)
    cid = rig.mod.card_id("incident", iid)
    rig.world.records[(HUB, CARDS)] = {cid: {"id": cid, "kind": "incident", "status": "dismissed"}}
    rig.run()
    assert rig.world.cards()[cid]["status"] == "dismissed"
    assert rig.world.incident("setup_blocked")["cardId"] == cid


def info_cards(rig):
    return [c for c in rig.world.cards().values() if c["kind"] == "info"]


def test_a_standup_only_for_a_run_that_did_something_and_the_daily_report_once():
    rig = Rig()
    with_card_stores(rig)
    rig.world.set_config("caretaker", {"lastReportDay": "2027-01-15", "noticeCooldownMin": 60})
    rig.run(at=T0)                                     # a healthy tick: nothing
    assert rig.world.standups() == {}
    rig.frozen.add("brain-facts")                      # it waits for its own 6 h run
    rig.fail_job("brain-facts", 3, stopped=True)
    rig.tick()
    (row,) = rig.world.standups().values()
    assert row["id"] == f"caretaker:{T0 + 900}" and row["job"] == "caretaker"
    assert any("opened 1 incident" in d for d in row["did"])
    assert any("tried enable_job" in d and "unverified" in d for d in row["did"])
    assert row["counts"]["repaired"] == 1 and row["needs"] == []
    rig.run(at=T0 + 1200)                              # repairing, waiting: nothing new
    assert len(rig.world.standups()) == 1
    # the first tick of the next UTC day reports the last 24 h, once
    next_day = (T0 // DAY + 1) * DAY + 60
    rig.run(at=next_day)
    (report,) = info_cards(rig)
    day = time.strftime("%Y-%m-%d", time.gmtime(next_day))
    assert report["title"] == f"Caretaker report {day}"
    assert report["subjectKey"] == f"caretaker:{day}"
    assert "1 incidents: 0 fixed (verified), 1 tried, unverified" in report["reason"]
    rig.run(at=next_day + 900)
    assert len(info_cards(rig)) == 1
    assert rig.world.config("caretaker")["lastReportDay"] == day


def test_the_report_counts_incidents_opened_or_closed_in_the_last_day_only():
    rig = Rig()
    with_card_stores(rig)
    del rig.world.triggers()["brain-setup"]
    rig.run(at=T0)                                     # escalated; it waits on the user
    day1 = (T0 // DAY + 1) * DAY + 60
    rig.run(at=day1)
    (report,) = info_cards(rig)
    assert "1 incidents: 0 fixed (verified), 0 tried, unverified, 1 escalated" in report["reason"]
    for n in (2, 3, 4):
        rig.run(at=day1 + (n - 1) * DAY)               # still escalated: no card every day
        assert len(info_cards(rig)) == 1
    assert rig.world.incident("setup_blocked")["status"] == "escalated"
    # an incident that closes a week after it opened is reported on the day it closed
    rig.world.triggers()["brain-setup"] = base_triggers()[-1]
    rig.run(at=day1 + 7 * DAY)
    assert len(info_cards(rig)) == 2
    assert "1 healed by themselves" in info_cards(rig)[-1]["reason"]


def test_a_quiet_day_writes_no_report():
    rig = Rig()
    with_card_stores(rig)
    rig.run()
    assert rig.world.cards() == {} and rig.world.standups() == {}
    assert rig.world.config("caretaker")["lastReportDay"] == time.strftime(
        "%Y-%m-%d", time.gmtime(T0))


def test_the_live_hub_datasets_win_over_the_jobs_args():
    rig = Rig()
    with_card_stores(rig)
    rig.cfg["brain.status"]["hub"]["objectId"] = "another-hub"
    del rig.world.triggers()["brain-setup"]
    rig.run()
    assert rig.world.cards() == {}                     # a different hub's map is not read


# --- mutation checks: each guard, deleted, turns its test red -----------------------

def mutant(old, new=""):
    assert SOURCE.count(old) == 1, old
    return SOURCE.replace(old, new)


def test_deleting_the_paused_sync_guard_is_caught():
    src = mutant("    if _mail_paused(snap):\n        return []\n")
    rig = Rig(cfg={"google.connected": False}, source=src)
    rig.world.trigger("brain-mail-sync")["enabled"] = False
    assert rig.run()["faults"] == 1


def test_deleting_the_uptime_base_is_caught():
    src = mutant('    return snap["now"] - max(since, snap["uptimeSince"])\n',
                 '    return snap["now"] - since\n')
    rig = Rig(source=src)
    rig.frozen.add("brain-facts")
    rig.world.trigger("brain-facts")["lastRunAt"] = T0 - 5 * 3600
    rig.run(at=T0)
    boot = T0 + 3600
    first = None
    for n in range(0, 60):
        if rig.run(at=boot + n * 900)["faults"] and first is None:
            first = boot + n * 900
    assert first is not None and first < boot + 49 * 900


def test_deleting_the_stopped_check_in_the_rule_table_is_caught():
    src = mutant('        if job is not None and job.get("lastStatus") == STOPPED '
                 'and not _enabled(job):\n',
                 '        if job is not None:\n')
    rig = Rig(source=src)
    rig.fail_job("brain-people", 2)
    rig.run()
    assert rig.world.signals() != []                   # the executor refuses what the rule allowed


def test_deleting_the_one_repair_guard_is_caught():
    src = mutant('            if inc.get("status") not in ("open", "escalated") '
                 'or inc.get("action") in REPAIRS:\n'
                 '                continue\n',
                 '            if inc.get("status") == "closed":\n'
                 '                continue\n')
    rig = Rig(source=src)
    overdue_people(rig)
    rig.run(at=T0)
    rig.tick()
    assert len(reruns(rig)) > 1


def test_a_restarted_job_opens_no_incident():
    # setup's on run flips `enabled` back on a stopped job; the runtime resets
    # the breaker in memory but the record keeps `auto_disabled` and its count
    # until the job's next run — six restarted jobs made six false incidents
    rig = Rig()
    rig.fail_job("brain-facts", 3, stopped=True)
    rig.world.trigger("brain-facts")["enabled"] = True
    rig.run()
    assert rig.world.incidents() == [] and rig.world.chat == []
    rig.tick()
    assert rig.world.incidents() == [] and rig.world.chat == []
    # stopped again after it ran and failed: a real fault, one incident
    rig.fail_job("brain-facts", 3, stopped=True)
    rig.tick()
    assert [i["faultClass"] for i in rig.world.incidents()] == ["job_failing"]
