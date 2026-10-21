"""programs/chain@v1 — per-item jobs in hops across runs (ADR-014 §7),
under the REAL guest kernel (kernelenv). Pinned: start validates and
arms hop 1 after the bar exists; a hop arms its successor BEFORE the
work and stamps it with the tally AFTER; a failed item is counted and
the hop goes on; a dead predecessor (no stamp) counts as unfinished
and three in a row stop the chain; the last hop finishes the bar and
posts the trigger:<job> summary; stop() disarms pending hops and a
disarmed successor is never rewritten."""

from kernelenv import load_kernel
from test_progress_module import FakeAny as ProgressFake

BAO = "bao"
STEP = '''"""A test step: records the call, fails on the item "bad"."""


def step(item, **kw):
    effect("test.step", {"item": item, "kw": kw})
    if item == "bad":
        raise ValueError("boom")
    if item == "stop":          # the user stops the chain mid-hop
        use("agent:chain@v1").stop("bao", "recipes")
'''


class FakeAny(ProgressFake):
    """The progress registry fake plus the trigger anchor's
    agent_triggers dataset (upsert / query / modify)."""

    def __init__(self, events):
        super().__init__()
        self.events = events          # shared with the step effect
        self.triggers = {}

    def bundle_child(self, space, bundle, seed, type_key=None):
        return {"objectId": "anchor"}

    def upsert_record(self, space, oid, ds, rid, value):
        assert (space, oid, ds) == (BAO, "anchor", "agent_triggers")
        self.triggers[rid] = {"id": rid, **value}
        self.events.append(("upsert", rid, value["args"]["prev"],
                            value["args"]["tally"]["done"]))
        return {}

    def query(self, space, oid, ds, **opts):
        recs = list(self.triggers.values())
        want = (opts.get("filter") or {}).get("id")
        if want is not None:
            recs = [r for r in recs if r["id"] == want]
        return recs

    def modify(self, space, body):
        for r in body["records"]:
            for op in r["ops"]:
                assert op["path"] == "enabled"
                self.triggers[r["id"]]["enabled"] = op["value"]
        return {}


def kernel():
    events = []
    fake = FakeAny(events)

    clock = [1000.0]

    def eff(name, payload):
        if name == "test.step":
            events.append(("step", payload["item"], payload["kw"]))
            return {}
        if name == "time.now":            # every read advances 1 s
            clock[0] += 1
            return {"epoch": clock[0]}
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=eff, any_client=fake,
                      module_source=lambda spec: STEP if spec == "step@v1" else None)
    return app.use("chain@v1"), fake, events


def test_start_validates_then_arms_hop_1_behind_the_bar():
    mod, fake, events = kernel()
    out = mod.start(BAO, "recipes", "step@v1", ["a", "b", "c"], batch=2,
                    args={"style": "x"})
    assert out == {"job": "recipes", "total": 3, "batch": 2, "hops": 2,
                   "gen": out["gen"], "triggerId": f"chain-recipes-g{out['gen']}-h1"}
    rec = fake.triggers[out["triggerId"]]
    assert rec["kind"] == "once" and rec["program"] == "agent:chain@v1"
    assert rec["enabled"] is True and rec["name"] == "recipes hop 1/2"
    a = rec["args"]
    assert a["chain"] is True and a["hop"] == 1 and a["prev"] is None
    assert a["items"] == ["a", "b", "c"] and a["args"] == {"style": "x"}
    assert a["method"] == "step" and a["space"] == BAO
    # the bar is registered with the total before the first hop exists
    assert fake.frames[0][0] == "register"
    assert fake.row("recipes.id-bao")["total"] == 3


def test_start_refuses_a_bad_job_items_batch_or_method():
    import pytest
    mod, fake, _ = kernel()
    with pytest.raises(ValueError, match="slug"):
        mod.start(BAO, "Recipes!", "step@v1", ["a"])
    with pytest.raises(ValueError, match="empty"):
        mod.start(BAO, "recipes", "step@v1", [])
    with pytest.raises(ValueError, match="batch"):
        mod.start(BAO, "recipes", "step@v1", ["a"], batch=0)
    with pytest.raises(ValueError, match="no callable"):
        mod.start(BAO, "recipes", "step@v1", ["a"], method="nope")
    assert fake.triggers == {} and fake.frames == []


def run_hop(mod, fake, hop):
    rec = fake.triggers[f"chain-recipes-g7-h{hop}"]
    return mod.main(rec["args"])


def test_hop_arms_its_successor_before_the_work_and_stamps_it_after():
    mod, fake, events = kernel()
    mod.start(BAO, "recipes", "step@v1", ["a", "bad", "c", "d", "e"], batch=2)
    fake.triggers = {k.replace(f"-g{v['args']['gen']}-", "-g7-"): v
                     for k, v in fake.triggers.items()}
    for v in fake.triggers.values():
        v["args"]["gen"] = 7
    events.clear()
    out = run_hop(mod, fake, 1)
    assert out == {"hop": 1, "hops": 3, "done": 1, "failed": 1,
                   "armed": "chain-recipes-g7-h2", "tally": out["tally"]}
    # order: arm (unstamped) → the two steps → the stamped rewrite
    assert [e[0] for e in events] == ["upsert", "step", "step", "upsert"]
    assert events[0][2] == {"hop": 1, "count": 2, "stamped": False}
    assert events[1][1:] == ("a", {}) and events[2][1] == "bad"
    assert events[3][2] == {"hop": 1, "count": 2, "stamped": True}
    t = fake.triggers["chain-recipes-g7-h2"]["args"]["tally"]
    assert t["done"] == 1 and t["failed"] == 1 and t["deadStreak"] == 0
    assert t["errors"] == [{"hop": 1, "item": "bad", "error": "ValueError: boom"}]
    assert fake.row("recipes.id-bao")["done"] == 2        # hop 1 ticked to 2/5
    assert fake.chats == []                                 # no summary yet


def test_last_hop_finishes_the_bar_and_posts_the_summary():
    mod, fake, events = kernel()
    mod.start(BAO, "recipes", "step@v1", ["a", "b", "c"], batch=2)
    for v in list(fake.triggers.values()):
        v["args"]["gen"] = 7
        fake.triggers["chain-recipes-g7-h1"] = v
    run_hop(mod, fake, 1)
    out = run_hop(mod, fake, 2)
    assert out["chainDone"] is True and out["stopped"] is None
    assert out["tally"]["done"] == 3 and out["tally"]["unfinished"] == 0
    assert "chain-recipes-g7-h3" not in fake.triggers
    row = fake.row("recipes.id-bao")
    assert row["state"] == "done" and row["done"] == 3
    assert row["message"] == "3/3 done, 0 failed, 0 unfinished"
    (space, chat, body), = fake.chats
    assert (space, chat) == (BAO, "chat1")
    assert body["agent"] == {"name": "trigger:recipes", "done": True}
    assert body["text"].startswith("[trigger: recipes] Chain 'recipes' FINISHED: "
                                   "3/3 done, 0 failed, 0 unfinished.")
    assert "do NOT chat_send" in body["text"]


def test_a_dead_predecessor_counts_as_unfinished_and_three_stop_the_chain():
    mod, fake, events = kernel()
    chain = {"job": "recipes", "program": "step@v1", "method": "step",
             "items": list("abcdefgh"), "args": {}, "batch": 2, "space": BAO,
             "agentSpace": BAO, "label": "recipes", "gen": 7}
    tally = {"done": 2, "failed": 0, "unfinished": 0, "deadStreak": 0, "errors": []}
    # hop 2 died (no stamp): hop 3 runs with stamped: False
    out = mod.main({**chain, "hop": 3, "tally": tally,
                    "prev": {"hop": 2, "count": 2, "stamped": False}})
    assert out["tally"]["unfinished"] == 2
    assert out["tally"]["deadStreak"] == 0      # hop 3 itself reported: streak over
    assert out["tally"]["errors"][0] == {
        "hop": 2, "error": "died before reporting; 2 item(s) unfinished"}
    assert out["done"] == 2 and out["armed"] == "chain-recipes-g7-h4"
    # the successor's record resets the streak after a hop that reported
    assert fake.triggers["chain-recipes-g7-h4"]["args"]["tally"]["deadStreak"] == 0
    # three dead in a row: the chain stops, the bar fails, the chat hears
    out = mod.main({**chain, "hop": 4, "prev": {"hop": 3, "count": 2, "stamped": False},
                    "tally": {**tally, "unfinished": 4, "deadStreak": 2}})
    assert out["chainDone"] is True and out["stopped"] == "3 hops in a row died"
    assert "chain-recipes-g7-h5" not in fake.triggers
    assert [e[0] for e in events] == ["upsert", "step", "step", "upsert"]  # hop 4 ran no step
    row = fake.row("recipes.id-bao")
    assert row["state"] == "failed" and row["error"] == {"message": "3 hops in a row died"}
    assert fake.chats[-1][2]["text"].startswith(
        "[trigger: recipes] Chain 'recipes' STOPPED: 2/8 done, 0 failed, 6 unfinished.")


def test_stop_disarms_pending_hops_and_a_disarmed_successor_is_not_rewritten():
    mod, fake, events = kernel()
    out = mod.start(BAO, "recipes", "step@v1", ["stop", "b", "c"], batch=1)
    gen = out["gen"]
    h1, h2 = f"chain-recipes-g{gen}-h1", f"chain-recipes-g{gen}-h2"
    fake.triggers["chain-other-g1-h1"] = {"id": "chain-other-g1-h1", "enabled": True}
    fake.triggers[h1]["enabled"] = False          # the scheduler consumed the shot
    events.clear()
    # hop 1 arms h2, then its step calls stop(): h2 is disarmed, the bar fails
    out = mod.main(fake.triggers[h1]["args"])
    assert out["armed"] == h2 and out["done"] == 1
    assert fake.triggers[h2]["enabled"] is False
    assert fake.triggers["chain-other-g1-h1"]["enabled"] is True    # another chain
    assert out["stopped"] == "chain stopped"
    row = fake.row("recipes.id-bao")     # the hop's own ticks reopened it; closed again
    assert row["state"] == "failed" and row["error"] == {"message": "chain stopped"}
    assert row["message"] == "stopped after hop 1/3: 1 done, 0 failed"
    assert fake.chats == []              # a stopped chain posts no summary
    # the disarmed successor is NOT rewritten after the work (that would re-arm it)
    assert [e[0] for e in events] == ["upsert", "step"]
    assert "prev" in fake.triggers[h2]["args"] and \
        fake.triggers[h2]["args"]["prev"]["stamped"] is False
    # nothing pending any more
    assert mod.stop(BAO, "recipes") == {"stopped": False, "disarmed": []}
