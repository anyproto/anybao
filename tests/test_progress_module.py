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
        self._n = 0

    def create_type(self, space, body):
        self.types_created.append(body["xKey"])
        return {"typeId": "T_" + body["xKey"]}

    def query_objects(self, space, filter=None, limit=None, **kw):
        f = filter or {}
        if f.get("any.types") == "agent-progress":
            return [dict(r) for r in self.rows]
        job = f.get("agent-progress.job")
        return [dict(r) for r in self.rows
                if r["agent-progress"]["job"] == job]

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
                            "current": 7, "startedAt": 900}},
        {"id": "stale", "modifiedAt": 10,
         "agent-progress": {"job": "j1", "label": "L", "status": "running",
                            "current": 3, "startedAt": 500}},
    ]


def test_start_publishes_full_baseline_before_work():
    fake = FakeAny()
    mod = load(fake)
    oid = mod.start(SP, "j1", "Import", total=10, program="p@v1")
    assert oid == "obj1" and fake.types_created == ["agent-progress"]
    p = fake.job()
    assert p["status"] == "running" and p["current"] == 0 and p["total"] == 10
    assert p["startedAt"] and p["updatedAt"] and p["error"] == ""
    assert p["program"] == "p@v1"


def test_start_converges_duplicates_freshest_wins_earliest_started_at():
    fake = FakeAny(rows=dup_rows())
    mod = load(fake)
    oid = mod.start(SP, "j1", "Import", total=10, current=7)
    assert oid == "fresh" and fake.deleted == ["stale"]
    p = fake.job()
    # rewritten in place, but the TRUE start (the stale racer's) is kept
    assert p["startedAt"] == 500 and p["current"] == 7


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


def test_fail_without_prior_start_creates_the_record():
    fake = FakeAny()
    mod = load(fake)
    mod.fail(SP, "j1", error="breaker open")
    p = fake.job()
    assert p["status"] == "failed" and p["error"] == "breaker open"
    assert fake.types_created == ["agent-progress"]
