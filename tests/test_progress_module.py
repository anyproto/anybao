"""programs/progress@v1 — the program-facing progress interface
(ADR-014), under the REAL guest kernel (kernelenv). Pinned: the
publish-before-work baseline, the duplicate-convergence dance
(freshest modifiedAt wins, stale rows deleted, earliest startedAt
carried), tick's property-write + self-heal, done's self-clean
delete, and fail keeping the object as the record."""

from kernelenv import load_kernel

SP = "space1"


class FakeAny:
    """The any@v1 surface progress@v1 touches, list-backed."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])   # [{id, modifiedAt, agent-progress}]
        self.types_created = []
        self.deleted = []
        self.records = []              # upsert_record calls
        self.chats = []                # chat_send calls (notify nudges)
        self._n = 0

    def create_type(self, space, body):
        self.types_created.append(body["xKey"])
        return {"typeId": "T_" + body["xKey"]}

    def query_objects(self, space, filter=None, limit=None, **kw):
        f = filter or {}
        if f.get("any.name") == "agent-triggers":
            return [{"id": "anchor-old", "createdAt": 10},
                    {"id": "anchor-new", "createdAt": 99}]  # oldest wins
        if f.get("any.types") == "agent-progress":
            return [dict(r) for r in self.rows]
        job = f.get("agent-progress.job")
        return [dict(r) for r in self.rows
                if r["agent-progress"]["job"] == job]

    def general_chat(self, space):
        return "chat1"

    def chat_send(self, space, chat_id, body):
        self.chats.append((space, chat_id, body))
        return {"id": f"msg{len(self.chats)}"}

    def upsert_record(self, space, object_id, dataset, record_id, value):
        self.records.append((space, object_id, dataset, record_id, value))
        return {}

    def create_object(self, space, body):
        self._n += 1
        oid = f"obj{self._n}"
        self.rows.append({
            "id": oid, "modifiedAt": 100 + self._n,
            "agent-progress": dict(body["initialProperties"]["agent-progress"])})
        return {"objectId": oid}

    def update_object(self, space, oid, body):
        for r in self.rows:
            if r["id"] == oid:
                r["agent-progress"].update(body["agent-progress"])
                r["modifiedAt"] = (r.get("modifiedAt") or 0) + 1
        return {"objectId": oid}

    def delete_object(self, space, oid):
        self.deleted.append(oid)
        self.rows = [r for r in self.rows if r["id"] != oid]
        return None

    def job(self, job="j1"):
        rows = [r for r in self.rows if r["agent-progress"]["job"] == job]
        assert len(rows) == 1, f"expected one {job} row, got {len(rows)}"
        return rows[0]["agent-progress"]


def clock_fx():
    t = {"n": 1000}

    def fx(name, payload):
        if name == "time.now":
            t["n"] += 1
            return {"epoch": t["n"]}
        raise AssertionError(f"unexpected effect {name}")
    return fx


def load(fake):
    app = load_kernel(effect=clock_fx(), any_client=fake)
    return app.use("progress@v1")


def dup_rows():
    """Two racing objects for one job — the stale one started EARLIER
    (its startedAt is the true start) but was written last long ago."""
    return [
        {"id": "fresh", "modifiedAt": 50,
         "agent-progress": {"job": "j1", "label": "L", "status": "running",
                            "current": 7, "started_at": 900}},
        {"id": "stale", "modifiedAt": 10,
         "agent-progress": {"job": "j1", "label": "L", "status": "running",
                            "current": 3, "started_at": 500}},
    ]


def test_start_publishes_full_baseline_before_work():
    fake = FakeAny()
    mod = load(fake)
    oid = mod.start(SP, "j1", "Import", total=10, program="p@v1")
    assert oid == "obj1" and fake.types_created == ["agent-progress"]
    p = fake.job()
    assert p["status"] == "running" and p["current"] == 0 and p["total"] == 10
    assert p["started_at"] and p["updated_at"] and p["error"] == ""
    assert p["program"] == "p@v1"


def test_start_converges_duplicates_freshest_wins_earliest_started_at():
    fake = FakeAny(rows=dup_rows())
    mod = load(fake)
    oid = mod.start(SP, "j1", "Import", total=10, current=7)
    assert oid == "fresh" and fake.deleted == ["stale"]
    p = fake.job()
    # rewritten in place, but the TRUE start (the stale racer's) is kept
    assert p["started_at"] == 500 and p["current"] == 7


def test_tick_is_a_property_write_and_reopens_after_fail():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10)
    mod.fail(SP, "j1", error="boom")
    assert fake.job()["status"] == "failed"
    mod.tick(SP, "j1", current=4, detail="hop 2")
    p = fake.job()
    assert p["status"] == "running" and p["current"] == 4
    assert p["detail"] == "hop 2" and p["error"] == "boom"  # error is history
    assert len(fake.rows) == 1                              # same object


def test_tick_self_heals_a_missing_object():
    fake = FakeAny()
    mod = load(fake)
    mod.tick(SP, "j1", current=2, total=8)
    p = fake.job()
    assert p["status"] == "running" and p["current"] == 2 and p["total"] == 8
    assert fake.types_created == ["agent-progress"]   # via start's ensure


def test_done_deletes_every_row_for_the_job():
    fake = FakeAny(rows=dup_rows())
    mod = load(fake)
    n = mod.done(SP, "j1")
    assert n == 2 and fake.rows == []
    assert mod.done(SP, "j1") == 0   # idempotent on nothing


def test_fail_keeps_the_object_as_the_record():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import")
    mod.fail(SP, "j1", error="quota", detail="hop 3")
    p = fake.job()
    assert p["status"] == "failed" and p["error"] == "quota"
    assert p["detail"] == "hop 3" and fake.deleted == []


def test_jobs_lists_one_freshest_row_per_job():
    fake = FakeAny(rows=dup_rows())
    mod = load(fake)
    mod.fail(SP, "j2", error="boom")
    out = mod.jobs(SP)
    by_job = {p["job"]: p for p in out}
    assert set(by_job) == {"j1", "j2"}          # dup j1 rows collapse to one
    assert by_job["j1"]["current"] == 7          # freshest modifiedAt wins
    assert by_job["j2"]["status"] == "failed"
    assert fake.deleted == []                    # jobs() is a pure getter, no pruning


def test_done_with_notify_posts_a_visible_trigger_message_with_counts():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10)
    mod.tick(SP, "j1", current=10)
    mod.done(SP, "j1", notify={"spaceId": "agentsp", "chatId": "chatX"})
    assert fake.rows == []                       # still self-cleans
    (aspace, chat, body) = fake.chats[-1]
    assert (aspace, chat) == ("agentsp", "chatX")
    # the foreign agent name is what makes the watcher treat it as input
    assert body["agent"]["name"] == "trigger:j1"
    assert "DONE" in body["text"]
    assert "10/10" in body["text"]               # captured before the delete
    assert body["text"].startswith("[trigger: progress]")


def test_fail_with_notify_posts_the_error_and_bare_space_form():
    fake = FakeAny()
    mod = load(fake)
    mod.fail(SP, "j1", error="quota", notify="agentsp")  # chat = general_chat
    (aspace, chat, body) = fake.chats[-1]
    assert (aspace, chat) == ("agentsp", "chat1")
    assert "FAILED: quota" in body["text"]
    assert body["agent"]["name"] == "trigger:j1"
    assert len(fake.rows) == 1                   # failed object still kept


def test_notify_is_best_effort_and_omitted_by_default():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import")
    mod.done(SP, "j1")
    assert fake.chats == []                      # no notify → no chat writes


def test_fail_without_prior_start_creates_the_record():
    fake = FakeAny()
    mod = load(fake)
    mod.fail(SP, "j1", error="breaker open")
    p = fake.job()
    assert p["status"] == "failed" and p["error"] == "breaker open"
    assert fake.types_created == ["agent-progress"]
