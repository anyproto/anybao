"""programs/brainCaretaker@v1 — the brain watching itself, under the REAL guest
kernel and the REAL llm@v1 (the model faked at its http.post). Pinned: the
pin check comes before any read; each grader's positive and negative case;
one incident per (class, subject), one never-updated observation per tick;
one model turn per incident, every reply rule, a model failure that
escalates and fails the run; the repairs write only their record and read
their target from the incident; the verify / soak ladder; self-heal leaves
`verified` absent; escalation cards only where the hub has `cards`;
batched, cooled-down notices under the agent's own name; the daily report.
The mutation checks at the end delete each guard and watch its test fail."""

import json
import time

import connectorenv
import kernelenv
import pytest
from brain_fakes import DATASETS, HUB, FakeSpace
from connectorenv import connector_kernel

BAO = "bao-space"
T0 = 1_800_000_000
DAY = 86400
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
SOURCE = (connectorenv.PROGRAMS_DIR / "brainCaretaker@v1" / "program.py").read_text()


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class ConfigError(Exception):
    """The broker's type for every config.get failure."""


class CredentialError(Exception):
    """A host effect failure that is not a provider status: llm@v1 re-raises
    it as an EffectError, unretried (a missing secret, a denied capability)."""


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
            "hub": {"objectId": HUB, "version": 6, "datasets": datasets or dict(DATASETS)},
            "mailSync": {"mode": "created", "triggerId": "brain-mail-sync", "other": []}}


class World(FakeSpace):
    """Bao's space with a healthy brain in it: every job ran a minute ago, mail
    synced ten minutes ago, the lists and tiers in place."""

    def __init__(self):
        super().__init__(BAO)
        self.chat = []
        self.active = "peer-a"
        self.seed_lists()
        for rec in base_triggers():
            self.records.setdefault(TRIGGERS, {})[rec["id"]] = rec
        self.set_config("promises.cursor", {"mb1": (T0 - 3600) * 1000})
        self.set_config("promises.stats", {"asked": 4, "providerFailed": 0})
        self.set_config("facts.cursor", {"watermark": T0 - DAY})
        self.set_config("people.cursor", {"mail": {"mb1": (T0 - 3600) * 1000}})
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


class Model:
    """The provider behind the caretaker's tier. Each queued reply is a dict
    (sent as JSON), a raw string, an int (an error status) or an exception
    (the effect fails); an empty queue escalates on the input's subject."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.inputs = []

    def answer(self, payload):
        user = [m for m in payload["json"]["messages"] if m["role"] == "user"][-1]["content"]
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
        seen = json.loads(text)
        self.inputs.append(seen)
        reply = self.replies.pop(0) if self.replies else {
            "hypothesis": "unclear", "confidence": 0.9, "evidence": [],
            "action": "escalate", "target": seen["subject"]}
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return {"status": reply, "headers": {}, "body": "provider says no"}
        if callable(reply):
            reply = reply(seen)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return {"status": 200, "headers": {}, "body": json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})}


def repair(action, *evidence, confidence=0.9, hypothesis="the job keeps failing"):
    """A reply that confirms the input's own subject as the target."""
    return lambda seen: {"hypothesis": hypothesis, "confidence": confidence,
                         "evidence": list(evidence), "action": action,
                         "target": seen["subject"]}


def base_cfg():
    return {"brain.space": {"spaceId": BAO, "requestedAt": REQUESTED, "by": "chat"},
            "brain.status": brain_status(),
            "llm.tier.brain.caretaker": TIER, "llm.tier.brain.extractor": TIER,
            "llm.tier.classify": TIER, "google.connected": True}


def caretaker_args():
    return {"space": BAO, "hub": HUB, "datasets": dict(DATASETS), "tier": "brain.caretaker"}


class Rig:
    """One kernel over one world, a clock, and the runtime's bookkeeping: before
    each run every job not in `frozen` ran a minute ago (the caretaker itself one
    tick ago) and mail synced ten minutes ago, unless `fresh=False`."""

    def __init__(self, world=None, cfg=None, model=None, source=None):
        self.world = world or World()
        self.cfg = {**base_cfg(), **(cfg or {})}
        self.model = model or Model()
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
        if name == "runtime.get":
            if payload["key"] == "agent.name" and "agent.name" in self.cfg:
                return {"value": self.cfg["agent.name"]}
            raise KeyError(f"no runtime value for {payload['key']!r}")
        if name == "oauth.status":
            return {"connected": self.cfg["google.connected"], "pending": False,
                    "scopes": [], "account": ME, "expiresAt": None}
        if name == "http.post":
            return self.model.answer(payload)
        if name == "sleep":
            return {}
        raise AssertionError(f"unexpected effect {name}")

    def run(self, at=None, fresh=True, args=None):
        if at is not None:
            self.clock["now"] = at
        now = self.clock["now"]
        if fresh:
            for tid, rec in self.world.triggers().items():
                if rec.get("kind") == "cron" and tid not in self.frozen:
                    rec["lastRunAt"] = now - (900 if rec["program"] == CARETAKER else 60)
            if "ss1" not in self.frozen:
                self.world.objects["ss1"]["modifiedAt"] = {"$date": (now - 600) * 1000}
        return self.mod.main(args if args is not None else caretaker_args())

    def fail_job(self, tid, failures, stopped=False, error="provider_failed"):
        rec = self.world.trigger(tid)
        rec.update(consecutiveFailures=failures, lastStatus="auto_disabled" if stopped else "error")
        if stopped:
            rec["enabled"] = False
        self.world.add_run(tid, self.clock["now"] - 60, status="FAILED", error=error)


def http_calls(rig):
    return [e for e in rig.effects if e[0] == "http.post"]


# --- the pin ----------------------------------------------------------------

def scenario_unpinned(rig):
    """→ (the raised type, the any@v1 calls, the effects) of a run with no tier row."""
    del rig.cfg["llm.tier.brain.caretaker"]
    with pytest.raises(Exception) as err:
        rig.run()
    return type(err.value).__name__, list(rig.world.calls), list(rig.effects)


def test_unpinned_is_raised_before_any_read():
    kind, calls, effects = scenario_unpinned(Rig())
    assert kind == "unpinned"
    assert calls == []
    assert effects == [("config.get", "llm.tier.brain.caretaker")]


def test_a_tier_row_without_a_model_is_unpinned():
    rig = Rig(cfg={"llm.tier.brain.caretaker": {"provider": "openai-compat"}})
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "unpinned" and rig.world.calls == []


def test_another_config_failure_is_not_unpinned():
    rig = Rig()
    real = rig._eff

    def unreachable(name, payload):
        if name == "config.get" and payload["key"] == "llm.tier.brain.caretaker":
            raise ConfigError('config store unreachable for "llm.tier.brain.caretaker": io')
        return real(name, payload)

    rig.mod = connector_kernel(effect=unreachable, any_client=rig.world).use("brainCaretaker@v1")
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "snapshot_failed" and rig.world.calls == []


def test_args_without_the_hub_are_unconfigured():
    rig = Rig()
    with pytest.raises(Exception) as err:
        rig.run(args={"space": BAO, "tier": "brain.caretaker"})
    assert type(err.value).__name__ == "unconfigured"
    assert rig.world.store("incidents") == {}


def test_unreadable_job_records_fail_the_snapshot():
    rig = Rig()
    rig.world.bundle_child = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "snapshot_failed"


# --- a healthy brain ----------------------------------------------------------

def test_a_healthy_brain_writes_nothing_but_its_own_state():
    rig = Rig()
    out = rig.run()
    assert out["faults"] == 0 and out["open"] == []
    assert rig.world.store("incidents") == {} and rig.world.store("observations") == {}
    assert http_calls(rig) == [] and rig.world.chat == []
    state = rig.world.config("caretaker")
    assert state["lastTickAt"] == T0 and state["noticeCooldownMin"] == 60
    assert set(state["watch"]) == {"brain-promises", "brain-facts"}


# --- the graders, from snapshots ----------------------------------------------

def snap(rig, **over):
    """A healthy snapshot, as `snapshot()` reads the World, with `over` applied."""
    s = {"now": T0, "space": BAO, "hub": HUB, "datasets": dict(DATASETS),
         "anchor": "triggers", "triggers": [dict(r) for r in base_triggers()],
         "runs": {}, "brainSpace": base_cfg()["brain.space"], "brainStatus": brain_status(),
         "exclusions": {"id": f"{HUB}/exclusions", "value": dict(EXCLUSIONS)},
         "tiers": {"brain.caretaker": TIER, "brain.extractor": TIER, "classify": TIER},
         "config": {}, "credential": True, "activeDevice": "peer-a", "cards": None,
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
    faults, _ = mod.grade(snap(Rig()), {})
    assert faults == {}


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


def test_a_paused_job_the_caretaker_itself_and_an_away_device_are_never_overdue():
    mod = Rig().mod
    paused = with_job(snap(Rig()), "brain-people", lastRunAt=T0 - DAY, enabled=False)
    assert mod.grade_job_overdue(paused) == []
    itself = with_job(snap(Rig()), "brain-caretaker", lastRunAt=T0 - 2000)
    assert mod.grade_job_overdue(with_job(itself, "brain-people", lastRunAt=T0 - 60)) == []
    # the caretaker's own previous run is past its limit: the device was away
    away = with_job(snap(Rig()), "brain-caretaker", lastRunAt=T0 - 2 * 900 - 121)
    assert mod.grade_job_overdue(with_job(away, "brain-people", lastRunAt=T0 - DAY)) == []


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
    # the unread run is passed on as "unavailable", never omitted
    s = with_job(snap(Rig()), "brain-people", consecutiveFailures=2)
    s["runs"]["brain-people"] = "unavailable"
    assert mod.grade_job_failing(s)[0]["evidence"]["lastErrorType"] == "unavailable"


def test_mail_stale():
    mod = Rig().mod
    (fault,) = mod.grade_mail_stale(snap(Rig(), credential=False))
    assert fault["subject"] == "mb1" and fault["evidence"]["credentialConnected"] is False
    assert fault["evidence"]["mailTriggerId"] == "brain-mail-sync"
    old = snap(Rig(), syncState={"id": "ss1", "modifiedAt": {"$date": (T0 - 6 * 3600 - 1) * 1000},
                                 "sync_state": {"mailbox_id": "mb1", "cursor": "4711",
                                                "skipped_count": 2}})
    (fault,) = mod.grade_mail_stale(old)
    assert fault["evidence"]["syncStateModifiedAt"] == T0 - 6 * 3600 - 1
    assert fault["evidence"]["cursor"] == "4711" and fault["evidence"]["skippedCount"] == 2
    assert mod.grade_mail_stale(snap(Rig())) == []
    assert mod.grade_mail_stale(snap(Rig(), credential="unavailable")) == []
    # away suppresses the age, not a dead credential
    away = with_job(old, "brain-caretaker", lastRunAt=T0 - 2 * 900 - 121)
    assert mod.grade_mail_stale(away) == []
    away["credential"] = False
    assert len(mod.grade_mail_stale(away)) == 1


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
    # the drafter's tier is checked once the hub has drafts
    mod = Rig().mod
    s = snap(Rig(), datasets={**DATASETS, "drafts": "hubtype_drafts"})
    s["tiers"]["brain.drafter"] = None
    assert classes(mod.grade_tier_unpinned(s)) == [("tier_unpinned", "llm.tier.brain.drafter")]
    assert mod._tiers_in_use(s["triggers"], s["datasets"]) == [
        "brain.caretaker", "brain.drafter", "brain.extractor", "classify"]


def test_setup_blocked():
    mod = Rig().mod
    waiting = {"spaceId": BAO, "requestedAt": iso(T0 - 3601), "by": "chat"}
    (fault,) = mod.grade_setup_blocked(snap(Rig(), brainSpace=waiting))
    assert fault["subject"] == "brain-setup" and fault["evidence"]["requestState"] == "waiting"
    assert fault["evidence"]["ageS"] == 3601 and fault["evidence"]["setupOwner"] == "peer-a"
    fresh = {**waiting, "requestedAt": iso(T0 - 1800)}
    assert mod.grade_setup_blocked(snap(Rig(), brainSpace=fresh)) == []
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


# --- the silent model stall ----------------------------------------------------

def promises_run(rig, at, failed=1, moved=False):
    """One brainPromises run the way #124's does it with a failing provider and
    one mailbox: success, providerFailed + 1, the cursor where it was."""
    rec = rig.world.trigger("brain-promises")
    rec.update(lastRunAt=at, lastStatus="ok")
    rig.world.add_run("brain-promises", at)
    stats = dict(rig.world.config("promises.stats"))
    stats["providerFailed"] += failed
    rig.world.set_config("promises.stats", stats)
    if moved:
        cursor = rig.world.config("promises.cursor")
        rig.world.set_config("promises.cursor", {"mb1": cursor["mb1"] + 1000})


def test_a_job_whose_model_fails_without_progress_is_stalled():
    rig = Rig()
    rig.frozen.add("brain-promises")
    rig.run(at=T0)
    promises_run(rig, T0 + 600)
    out = rig.run(at=T0 + 900)
    assert out["faults"] == 0 and rig.world.config("caretaker")["watch"][
        "brain-promises"]["streak"] == 1
    out = rig.run(at=T0 + 1800)                        # no run since: the streak holds
    assert out["faults"] == 0
    promises_run(rig, T0 + 2400)
    rig.run(at=T0 + 2700)
    inc = rig.world.incident("model_stalled")
    assert inc["subject"] == "brain-promises" and inc["status"] in ("escalated", "repairing")
    (obs,) = rig.world.observations(inc["id"])
    assert obs["evidence"] == {"streak": 2, "providerFailedDelta": 1, "providerFailedTotal": 2,
                               "cursorMoved": False, "lastRunAt": T0 + 2400,
                               "lastRunStatus": "ok"}


def test_a_run_that_moves_its_cursor_resets_the_stall():
    rig = Rig()
    rig.frozen.add("brain-promises")
    rig.run(at=T0)
    promises_run(rig, T0 + 600)
    rig.run(at=T0 + 900)
    promises_run(rig, T0 + 1200, moved=True)           # one failure, but progress
    rig.run(at=T0 + 1500)
    assert rig.world.config("caretaker")["watch"]["brain-promises"]["streak"] == 0
    promises_run(rig, T0 + 1800, failed=0)
    rig.run(at=T0 + 2100)
    assert rig.world.store("incidents") == {}


# --- incidents and observations -------------------------------------------------

def test_one_incident_per_fault_and_one_never_updated_observation_per_tick():
    rig = Rig(model=Model(repair("notice", "consecutiveFailures"),))
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)
    rig.run(at=T0 + 900)
    (inc,) = rig.world.incidents()
    assert inc["faultClass"] == "job_failing" and inc["subject"] == "brain-people"
    assert inc["openedAt"] == {"$date": T0 * 1000}
    assert inc["id"] == rig.mod.incident_id("job_failing", "brain-people", T0)
    obs = rig.world.observations(inc["id"])
    assert [o["id"] for o in obs] == [f"{inc['id']}:{T0}", f"{inc['id']}:{T0 + 900}"]
    assert obs[0]["evidence"]["consecutiveFailures"] == 2
    # observations go through upsert_records only: never a modify
    assert not [c for c in rig.world.calls if c[0] == "modify" and c[1] == "observations"]
    assert [c for c in rig.world.calls if c[0] == "upsert_records" and c[1] == "observations"]


def test_a_fault_that_clears_by_itself_closes_with_verified_absent():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.cfg["llm.tier.brain.caretaker"] = TIER
    rig.model.replies = []
    rig.mod.MAX_CALLS = 0                              # no model turn: the incident stays open
    rig.run(at=T0)
    assert rig.world.incident("job_failing")["status"] == "open"
    rig.world.trigger("brain-people").update(consecutiveFailures=0, lastStatus="ok")
    rig.run(at=T0 + 900)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "closed" and inc["closedAt"] == {"$date": (T0 + 900) * 1000}
    assert "verified" not in inc
    assert rig.world.observations(inc["id"])[-1]["evidence"] == {"clean": True}
    rig.run(at=T0 + 1800)                              # closed: no more observations
    assert len(rig.world.observations(inc["id"])) == 2


# --- the model turn -------------------------------------------------------------

def test_the_model_turn_gets_the_evidence_history_and_allowed_actions():
    rig = Rig(model=Model(repair("enable_job", "lastStatus")))
    rig.fail_job("brain-facts", 3, stopped=True, error="unpinned")
    rig.run()
    (seen,) = rig.model.inputs
    assert seen["faultClass"] == "job_failing" and seen["subject"] == "brain-facts"
    assert seen["allowed"] == ["run_job", "enable_job", "rewind_cursor", "notice", "escalate"]
    assert seen["evidence"]["lastErrorType"] == "unpinned"
    assert seen["history"] == [{"tick": T0, "evidence": seen["evidence"]}]
    assert "stopped after repeated failures" in seen["summary"]
    inc = rig.world.incident("job_failing")
    assert inc["action"] == "enable_job" and inc["confidence"] == 0.9
    assert inc["evidence"] == ["lastStatus"] and inc["hypothesis"] == "the job keeps failing"
    assert inc["model"] == "codex/gpt-5.5" and inc["modelTurnAt"] == {"$date": T0 * 1000}


def test_one_turn_per_incident_and_at_most_five_calls_a_run():
    rig = Rig()
    for tid in ("brain-heartbeat", "brain-people", "brain-promises", "brain-facts",
                "brain-reconcile"):
        rig.fail_job(tid, 2)
    del rig.cfg["llm.tier.classify"]                   # a sixth incident
    rig.run(at=T0)
    assert len(http_calls(rig)) == 5
    assert sum(i["status"] == "open" for i in rig.world.incidents()) == 1
    rig.run(at=T0 + 900)
    assert len(http_calls(rig)) == 6                   # the sixth, one tick later
    rig.run(at=T0 + 1800)
    assert len(http_calls(rig)) == 6                   # and none again


def test_a_recent_turn_is_not_repeated_within_the_hour():
    rig = Rig()
    rig.fail_job("brain-people", 2)
    rig.mod.MAX_CALLS = 0
    rig.run(at=T0)
    iid = rig.world.incident("job_failing")["id"]
    rig.world.store("incidents")[iid]["modelTurnAt"] = {"$date": (T0 + 100) * 1000}
    rig.mod.MAX_CALLS = 5
    rig.run(at=T0 + 900)
    assert http_calls(rig) == []
    rig.run(at=T0 + 100 + 3600)
    assert len(http_calls(rig)) == 1


def run_reply(reply, fail="brain-people", **cfg):
    """One tick with one failing job and the model answering `reply`."""
    rig = Rig(cfg=cfg, model=Model(reply))
    rig.fail_job(fail, 2)
    rig.run()
    return rig


def reruns(rig):
    return [t for t in rig.world.triggers() if t.startswith("brain-rerun-")]


def test_a_word_confidence_is_zero_and_escalates():
    rig = run_reply(repair("run_job", "consecutiveFailures", confidence="high"))
    inc = rig.world.incident("job_failing")
    assert inc["confidence"] == 0 and inc["action"] == "escalate"
    assert inc["status"] == "escalated" and reruns(rig) == [] and rig.world.signals() == []


def test_an_unsure_turn_escalates_rather_than_repairs():
    rig = run_reply(repair("run_job", "consecutiveFailures", confidence=0.4))
    assert rig.world.incident("job_failing")["action"] == "escalate" and reruns(rig) == []


def refused(rig):
    (sig,) = rig.world.signals()
    inc = rig.world.incident("job_failing")
    assert sig["kind"] == "repair_refused" and sig["decision"] == "refused"
    assert sig["subject"] == inc["id"] and sig["tick"] == T0
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    assert reruns(rig) == [] and "hypothesis" not in inc
    return sig["reason"]


def scenario_unknown_evidence(rig):
    rig.model.replies = [repair("run_job", "consecutiveFailures", "theUsersPassword")]
    rig.fail_job("brain-people", 2)
    rig.run()
    return reruns(rig), rig.world.signals()


def test_an_unknown_evidence_key_is_refused():
    rig = Rig()
    assert scenario_unknown_evidence(rig)[0] == []
    assert "evidence" in refused(rig)


def test_an_action_outside_the_allowlist_is_refused():
    rig = run_reply(repair("delete_job", "consecutiveFailures"))
    assert "not allowed" in refused(rig)
    # an allowed repair that does not apply here is refused too: the job is not stopped
    rig = run_reply(repair("enable_job", "consecutiveFailures"))
    assert "not allowed" in refused(rig)


def scenario_rewind_an_overdue_job(rig):
    """An overdue job may be rerun or noticed, never rewound → its cursor after."""
    rig.model.replies = [repair("rewind_cursor", "lastRunAt")]
    rig.frozen.add("brain-promises")
    rig.world.trigger("brain-promises")["lastRunAt"] = T0 - 2 * 1800 - 300
    rig.run()
    return rig.world.config("promises.cursor")


def test_a_repair_another_class_allows_is_refused_here():
    rig = Rig()
    assert scenario_rewind_an_overdue_job(rig) == {"mb1": (T0 - 3600) * 1000}
    (sig,) = rig.world.signals()
    assert "not allowed" in sig["reason"]
    assert rig.world.incident("job_overdue")["status"] == "escalated"


def scenario_wrong_target(rig):
    rig.model.replies = [lambda seen: {"hypothesis": "h", "confidence": 0.9,
                                       "evidence": ["consecutiveFailures"],
                                       "action": "run_job", "target": "brain-facts"}]
    rig.fail_job("brain-people", 2)
    rig.run()
    return reruns(rig), rig.world.signals()


def test_a_target_other_than_the_subject_is_refused():
    rig = Rig()
    assert scenario_wrong_target(rig)[0] == []
    assert "target" in refused(rig)


def test_an_unparsable_reply_escalates():
    rig = run_reply("I think you should rerun it.")
    assert refused(rig) == "unparsable reply"
    inc = rig.world.incident("job_failing")
    assert inc["model"] == "codex/gpt-5.5" and inc["modelTurnAt"] == {"$date": T0 * 1000}


def test_validate_reply_rules():
    mod = Rig().mod
    ev = {"lastStatus": "error", "consecutiveFailures": 2}
    allowed = ["run_job", "notice"]

    def check(reply):
        return mod.validate_reply(json.dumps(reply), "brain-people", ev, allowed)

    good = {"hypothesis": " h ", "confidence": 1, "evidence": ["lastStatus", "history"],
            "action": "run_job", "target": "brain-people"}
    assert check(good) == {"accepted": True, "action": "run_job", "hypothesis": "h",
                           "confidence": 1, "evidence": ["lastStatus", "history"]}
    for confidence in ("0.9", True, 1.5, -0.1, None):
        assert check({**good, "confidence": confidence})["action"] == "escalate"
    assert check({**good, "evidence": "lastStatus"})["accepted"] is False
    assert check({**good, "evidence": []})["accepted"] is False        # a repair cites
    assert check({**good, "evidence": [], "action": "escalate"})["accepted"] is True
    assert check({**good, "target": None})["accepted"] is False
    assert check({**good, "action": "escalate", "target": "x"})["accepted"] is False
    assert mod.validate_reply("[]", "brain-people", ev, allowed)["accepted"] is False
    long = check({**good, "hypothesis": "x" * 900})["hypothesis"]
    assert len(long) == 500


# --- a model failure is not success ---------------------------------------------

def test_a_failing_effect_escalates_and_fails_the_run_after_its_writes():
    rig = Rig(model=Model(CredentialError("missing secret llm.key.anyai")))
    rig.fail_job("brain-people", 2)
    with pytest.raises(Exception) as err:
        rig.run()
    assert type(err.value).__name__ == "provider_failed"
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    assert len(rig.world.observations(inc["id"])) == 1
    assert rig.world.config("caretaker")["lastTickAt"] == T0
    (_, body), = rig.world.chat
    assert "people" in body["text"]


def test_a_provider_status_escalates_too_and_one_good_call_keeps_the_run_ok():
    rig = Rig(model=Model(401, repair("notice", "consecutiveFailures")))
    rig.fail_job("brain-people", 2)
    rig.fail_job("brain-promises", 2)
    rig.run()                                          # one of two calls failed: no raise
    statuses = sorted(i["status"] for i in rig.world.incidents())
    assert statuses == ["escalated", "repairing"]


# --- the repairs ----------------------------------------------------------------

def test_run_job_arms_a_once_trigger_with_the_jobs_program_and_args():
    rig = run_reply(repair("run_job", "consecutiveFailures"))
    (rid,) = reruns(rig)
    assert rid == f"brain-rerun-brain-people-{T0}"
    rerun, job = rig.world.trigger(rid), rig.world.trigger("brain-people")
    assert rerun["kind"] == "once" and rerun["spec"] == {"at": T0} and rerun["enabled"] is True
    assert rerun["program"] == PEOPLE and rerun["args"] == job["args"]
    assert "lastRunAt" not in rerun and "owner" not in rerun
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "repairing" and inc["actionAt"] == {"$date": T0 * 1000}
    assert inc["verifyAt"] == {"$date": (T0 + 900) * 1000}


def test_a_taken_rerun_id_gets_a_fresh_one():
    rig = Rig(model=Model(repair("run_job", "consecutiveFailures")))
    rig.world.triggers()[f"brain-rerun-brain-people-{T0}"] = {
        "id": f"brain-rerun-brain-people-{T0}", "kind": "once", "program": PEOPLE}
    rig.fail_job("brain-people", 2)
    rig.run()
    assert f"brain-rerun-brain-people-{T0}-2" in rig.world.triggers()


def test_enable_job_writes_enabled_true_on_the_jobs_record_and_nothing_else():
    rig = Rig(model=Model(repair("enable_job", "lastStatus")))
    rig.fail_job("brain-facts", 3, stopped=True)
    before = {k: dict(v) for k, v in rig.world.triggers().items()}
    rig.run()
    modifies = [c for c in rig.world.calls if c[0] == "modify" and c[1] == "agent_triggers"]
    assert modifies == [("modify", "agent_triggers", "brain-facts", ["enabled"])]
    after = rig.world.triggers()
    assert after["brain-facts"] == {**before["brain-facts"], "enabled": True}
    assert set(after) == set(before)


def test_enable_job_is_offered_only_for_a_brain_job_the_runtime_stopped():
    mod = Rig().mod
    s = with_job(snap(Rig()), "brain-facts", lastStatus="auto_disabled", enabled=False)
    assert "enable_job" in mod.allowed_actions("job_failing", "brain-facts", s)
    assert "enable_job" not in mod.allowed_actions("job_failing", "brain-people", s)
    # the mail sync is not a brain job: no repair of its record
    assert mod.allowed_actions("job_failing", "brain-mail-sync", s) == ["notice"]
    assert mod.allowed_actions("setup_blocked", "brain-setup", s) == []
    assert mod.allowed_actions("mail_stale", "mb1", s) == ["run_job", "notice"]
    assert mod.allowed_actions("exclusions_unreadable", "config/exclusions", s) == ["notice"]


def test_rewind_cursor_moves_the_jobs_cursor_a_day_back_once():
    rig = Rig(model=Model(repair("rewind_cursor", "lastErrorType")))
    rig.fail_job("brain-promises", 2)
    rig.run(at=T0)
    assert rig.world.config("promises.cursor") == {"mb1": (T0 - 3600 - DAY) * 1000}
    assert rig.world.incident("job_failing")["status"] == "repairing"
    rig.run(at=T0 + 900)                               # still failing: escalated, no rewind
    assert rig.world.config("promises.cursor") == {"mb1": (T0 - 3600 - DAY) * 1000}
    assert rig.world.incident("job_failing")["status"] == "escalated"
    assert len(http_calls(rig)) == 1


def test_an_unreadable_cursor_refuses_the_rewind():
    rig = Rig(model=Model(repair("rewind_cursor", "lastErrorType")))
    rig.world.set_config("facts.cursor", "garbled")
    rig.fail_job("brain-facts", 2)
    rig.run()
    (sig,) = rig.world.signals()
    assert "facts.cursor" in sig["reason"]
    assert rig.world.config("facts.cursor") == "garbled"
    assert rig.world.incident("job_failing")["status"] == "escalated"


def test_rewind_rules():
    mod = Rig().mod
    assert mod._rewind_people({"mail": {"a": DAY * 1000 * 2}, "calendar": "x"}) == {
        "mail": {"a": DAY * 1000}, "calendar": "x"}
    assert mod._rewind_facts({"watermark": 2 * DAY}) == {"watermark": DAY}
    assert mod._back_ms({"a": "x"}) is None and mod._rewind_people({}) is None
    assert GMAIL_SYNC not in mod.REWIND


def test_a_notice_is_one_batched_message_under_the_agents_name_per_cooldown():
    rig = Rig(model=Model(repair("notice", "consecutiveFailures"),
                          repair("notice", "consecutiveFailures")))
    rig.fail_job("brain-people", 2)
    rig.fail_job("brain-promises", 2)
    rig.run(at=T0)
    (chat_id, body), = rig.world.chat
    assert chat_id == "chat-1" and body["agent"] == {"name": "bao", "done": True}
    assert body["text"].count("\n• ") == 2 and "brain people failed 2 times" in body["text"]
    # a third incident inside the cooldown waits; after it, one more message
    del rig.cfg["llm.tier.classify"]
    rig.run(at=T0 + 900)
    assert len(rig.world.chat) == 1 and rig.world.config("caretaker")["pending"]
    rig.cfg["agent.name"] = "Bobrik"
    rig.run(at=T0 + 3600)
    assert len(rig.world.chat) == 2 and rig.world.chat[1][1]["agent"]["name"] == "Bobrik"
    assert rig.world.config("caretaker")["pending"] == []


def test_mail_stale_reruns_the_mail_sync_and_a_dead_credential_is_noticed():
    rig = Rig(cfg={"google.connected": False}, model=Model(repair("run_job",
                                                                  "credentialConnected")))
    rig.run()
    inc = rig.world.incident("mail_stale")
    assert inc["subject"] == "mb1"
    (rid,) = reruns(rig)
    assert rid == f"brain-rerun-brain-mail-sync-{T0}"
    assert rig.world.trigger(rid)["program"] == GMAIL_SYNC
    assert rig.world.trigger(rid)["args"] == {"space": BAO}


# --- the ladder -------------------------------------------------------------------

def test_a_repair_is_verified_then_soaked_before_it_is_fixed():
    rig = Rig(model=Model(repair("run_job", "consecutiveFailures")))
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)
    rig.world.trigger("brain-people").update(consecutiveFailures=0, lastStatus="ok")
    rig.run(at=T0 + 600)                               # clean, but before verifyAt
    assert rig.world.incident("job_failing")["status"] == "repairing"
    rig.run(at=T0 + 900)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "soaking" and inc["verifyAt"] == {"$date": (T0 + 900 + 3600) * 1000}
    rig.run(at=T0 + 1800)
    assert rig.world.incident("job_failing")["status"] == "soaking"
    rig.run(at=T0 + 900 + 3600)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "closed" and inc["verified"] is True
    assert inc["closedAt"] == {"$date": (T0 + 4500) * 1000}
    assert len(http_calls(rig)) == 1 and rig.world.chat == []   # success is silent


def test_a_repair_that_does_not_hold_escalates_once_with_no_second_repair():
    rig = Rig(model=Model(repair("run_job", "consecutiveFailures")))
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)
    rig.run(at=T0 + 900)                               # still failing at verifyAt
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["verified"] is False
    assert inc["action"] == "run_job"                  # what it tried stays on the record
    assert len(rig.world.chat) == 1
    for tick in (T0 + 1800, T0 + 5400, T0 + 9000):
        rig.run(at=tick)
    assert len(http_calls(rig)) == 1 and len(reruns(rig)) == 1 and len(rig.world.chat) == 1
    rig.world.trigger("brain-people").update(consecutiveFailures=0, lastStatus="ok")
    rig.run(at=T0 + 9900)                              # then it clears: closed, still unverified
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "closed" and inc["verified"] is False


def test_a_dirty_tick_during_the_soak_escalates():
    rig = Rig(model=Model(repair("run_job", "consecutiveFailures")))
    rig.fail_job("brain-people", 2)
    rig.run(at=T0)
    rig.world.trigger("brain-people").update(consecutiveFailures=0, lastStatus="ok")
    rig.run(at=T0 + 900)
    rig.world.trigger("brain-people").update(consecutiveFailures=2, lastStatus="error")
    rig.run(at=T0 + 1800)
    inc = rig.world.incident("job_failing")
    assert inc["status"] == "escalated" and inc["verified"] is False


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
    assert mod.step({"status": "repairing", "verifyAt": v}, T0, False) == {
        "status": "escalated", "verified": False}
    assert mod.step({"status": "soaking", "verifyAt": v}, T0 - 1, True) is None
    assert mod.step({"status": "soaking", "verifyAt": v}, T0 - 1, False)["verified"] is False
    assert mod.step({"status": "soaking", "verifyAt": v}, T0, True)["verified"] is True
    assert mod.step({"status": "closed"}, T0, True) is None


# --- escalation, cards, the report -------------------------------------------------

def with_phase3_stores(rig):
    datasets = {**DATASETS, "cards": CARDS, "standups": STANDUPS}
    rig.cfg["brain.status"] = brain_status(datasets)


def test_setup_blocked_escalates_without_a_model_turn():
    rig = Rig()
    with_phase3_stores(rig)
    del rig.world.triggers()["brain-setup"]
    rig.run()
    inc = rig.world.incident("setup_blocked")
    assert inc["status"] == "escalated" and inc["action"] == "escalate"
    assert http_calls(rig) == [] and "modelTurnAt" not in inc
    (card,) = [c for c in rig.world.cards().values() if c["kind"] == "incident"]
    assert card["kind"] == "incident" and card["subjectKey"] == inc["id"]
    assert card["title"] == "setup_blocked: brain-setup" and card["status"] == "open"
    assert card["id"] == rig.mod.card_id("incident", inc["id"]) == inc["cardId"]
    assert "brain-setup job is gone" in card["reason"]
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
    with_phase3_stores(rig)
    del rig.world.triggers()["brain-setup"]
    iid = rig.mod.incident_id("setup_blocked", "brain-setup", T0)
    cid = rig.mod.card_id("incident", iid)
    rig.world.records[(HUB, CARDS)] = {cid: {"id": cid, "kind": "incident", "status": "dismissed"}}
    rig.run()
    assert rig.world.cards()[cid]["status"] == "dismissed"
    assert rig.world.incident("setup_blocked")["cardId"] == cid


def test_a_standup_only_for_a_run_that_did_something_and_the_daily_report_once():
    rig = Rig(model=Model(repair("run_job", "consecutiveFailures")))
    with_phase3_stores(rig)
    rig.world.set_config("caretaker", {"lastReportDay": "2027-01-15", "noticeCooldownMin": 60})
    rig.run(at=T0)                                     # a healthy tick: nothing
    assert rig.world.standups() == {}
    rig.fail_job("brain-people", 2)
    rig.run(at=T0 + 900)
    (row,) = rig.world.standups().values()
    assert row["id"] == f"caretaker:{T0 + 900}" and row["job"] == "caretaker"
    assert any("opened 1 incident" in d for d in row["did"])
    assert any("unverified" in d for d in row["did"]) and row["counts"]["repaired"] == 1
    rig.run(at=T0 + 1200)                              # repairing, waiting: nothing new
    assert len(rig.world.standups()) == 1
    # the first tick of the next UTC day reports the last 24 h, once
    next_day = (T0 // DAY + 1) * DAY + 60
    rig.world.trigger("brain-people").update(consecutiveFailures=0, lastStatus="ok")
    rig.run(at=next_day)
    reports = [c for c in rig.world.cards().values() if c["kind"] == "info"]
    (report,) = reports
    day = time.strftime("%Y-%m-%d", time.gmtime(next_day))
    assert report["title"] == f"Caretaker report {day}"
    assert report["subjectKey"] == f"caretaker:{day}"
    assert "1 incidents: 0 fixed (verified), 1 tried, unverified" in report["reason"]
    rig.run(at=next_day + 900)
    assert len([c for c in rig.world.cards().values() if c["kind"] == "info"]) == 1
    assert rig.world.config("caretaker")["lastReportDay"] == day


def test_a_quiet_day_writes_no_report():
    rig = Rig()
    with_phase3_stores(rig)
    rig.run()
    assert rig.world.cards() == {} and rig.world.standups() == {}
    assert rig.world.config("caretaker")["lastReportDay"] == time.strftime(
        "%Y-%m-%d", time.gmtime(T0))


def test_the_live_hub_datasets_win_over_the_jobs_args():
    rig = Rig()
    with_phase3_stores(rig)
    rig.cfg["brain.status"]["hub"]["objectId"] = "another-hub"
    del rig.world.triggers()["brain-setup"]
    rig.run()
    assert rig.world.cards() == {}                     # a different hub's map is not read


# --- mutation checks: each guard, deleted, turns its test red -----------------------

def mutant(old, new=""):
    assert SOURCE.count(old) == 1, old
    return SOURCE.replace(old, new)


def test_deleting_the_pin_check_is_caught():
    src = mutant("    _pinned(tier)   # the first statement: no read, no write before the pin "
                 "(ADR-038 §1)\n")
    kind, calls, effects = scenario_unpinned(Rig(source=src))
    assert (kind, calls, effects) != ("unpinned", [], [("config.get", "llm.tier.brain.caretaker")])


def test_deleting_the_target_check_is_caught():
    src = mutant('    if reply.get("target") != subject:\n'
                 '        return _refused("the target is not the incident\'s subject")\n')
    runs, signals = scenario_wrong_target(Rig(source=src))
    assert runs != [] and signals == []


def test_deleting_the_evidence_key_check_is_caught():
    src = mutant("    if not isinstance(cited, list) or not all(isinstance(k, str) and k in known"
                 " for k in cited):\n",
                 "    if not isinstance(cited, list):\n")
    runs, signals = scenario_unknown_evidence(Rig(source=src))
    assert runs != [] and signals == []


def test_deleting_the_allowlist_check_is_caught():
    src = mutant('    if action != "escalate" and action not in allowed:\n'
                 '        return _refused(f"action {str(action)[:40]!r} is not allowed here")\n')
    assert scenario_rewind_an_overdue_job(Rig(source=src)) != {"mb1": (T0 - 3600) * 1000}


def test_deleting_the_confidence_floor_is_caught():
    src = mutant('    if action != "escalate" and confidence < CONFIDENCE_MIN:\n'
                 '        action = "escalate"                 # an unsure turn never repairs\n')
    rig = Rig(source=src, model=Model(repair("run_job", "consecutiveFailures",
                                             confidence="high")))
    rig.fail_job("brain-people", 2)
    rig.run()
    assert reruns(rig) != []
