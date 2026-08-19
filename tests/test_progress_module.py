"""programs/progress@v1 — the program-facing progress interface
(ADR-014, transport = the server process registry since the §2 swap),
under the REAL guest kernel (kernelenv). Pinned: register+baseline
before work, the (job, space)→process-id mapping, tick's fold +
heartbeat + 404 self-heal, terminal done/failed frames (linger view,
error cleared on reopen), notify nudges, and the silent no-op against
a server without the facility (pre-#163)."""

from kernelenv import load_kernel

SP = "space1"
PID = "j1.id-space1"        # f"{job}.{sid}" — FakeAny maps sid = "id-"+space


class WireError(Exception):
    """Raise-able with wire attrs; kernelenv relays status/code so the
    guest's AnyError branches see the real values."""

    def __init__(self, status, code, message=""):
        self.status = status
        self.code = code
        super().__init__(message or code)


class FakeAny:
    """The any@v1 surface progress@v1 touches: the process registry.

    Models the #163 contract bits the module relies on: re-register
    restarts a row; progress folds (absent keeps, explicit sets,
    total 0 → unknown) and resurrects a terminal row to running with
    the error cleared; finish stores the terminal state (rows linger
    in the view); missing rows 404 process.not_found. facility=False
    models a pre-#163 server: every route 404s request.not_found."""

    def __init__(self, facility=True):
        self.facility = facility
        self.processes = {}     # pid -> row
        self.frames = []        # every register/progress/finish, in order
        self.chats = []         # chat_send calls (notify nudges)

    def _gate(self):
        if not self.facility:
            raise WireError(404, "request.not_found", "Not Found")

    def get_space(self, space):
        return {"id": f"id-{space}"}

    def _process_register(self, body):
        self._gate()
        self.frames.append(("register", dict(body)))
        self.processes[body["id"]] = {
            "identity": "acct1", "self": True, "id": body["id"],
            "kind": body["kind"], "title": body["title"],
            "scope": body["scope"], "target": body.get("target"),
            "state": "running", "done": 0, "total": None, "message": "",
            "error": None}
        return {"subscribers": 1}

    def _process_progress(self, pid, body):
        self._gate()
        if pid not in self.processes:
            raise WireError(404, "process.not_found", "register first")
        self.frames.append(("progress", pid, dict(body)))
        row = self.processes[pid]
        if "done" in body:
            row["done"] = body["done"]
        if "total" in body:
            row["total"] = body["total"] or None   # 0 → back to unknown
        if "message" in body:
            row["message"] = body["message"]
        row["state"], row["error"] = "running", None   # resurrect semantics
        return {"subscribers": 1}

    def _process_finish(self, pid, body):
        self._gate()
        if pid not in self.processes:
            raise WireError(404, "process.not_found", "register first")
        self.frames.append(("finish", pid, dict(body)))
        row = self.processes[pid]
        row["state"] = body["status"]
        row["error"] = body.get("error")
        return {"subscribers": 1}

    def list_processes(self):
        self._gate()
        return [dict(r) for r in self.processes.values()]

    def general_chat(self, space):
        return "chat1"

    def chat_send(self, space, chat_id, body):
        self.chats.append((space, chat_id, body))
        return {"id": f"msg{len(self.chats)}"}

    def row(self, pid=PID):
        assert pid in self.processes, f"no process {pid}"
        return self.processes[pid]


def load(fake):
    app = load_kernel(any_client=fake)
    return app.use("progress@v1")


def test_start_registers_and_publishes_baseline_before_work():
    fake = FakeAny()
    mod = load(fake)
    pid = mod.start(SP, "j1", "Import", total=10, program="p@v1")
    assert pid == PID
    r = fake.row()
    assert r["state"] == "running" and r["done"] == 0 and r["total"] == 10
    assert r["title"] == "Import" and r["kind"] == "agent"
    assert r["target"] == "id-space1"          # subject = the space
    # register precedes the counter frame (bar exists before work)
    assert [f[0] for f in fake.frames] == ["register", "progress"]


def test_start_is_the_owner_restart_path():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10)
    mod.tick(SP, "j1", current=7)
    mod.start(SP, "j1", "Import", total=10, current=7)   # resume/retry
    r = fake.row()
    assert r["state"] == "running" and r["done"] == 7    # same bar, restarted


def test_tick_folds_and_reopens_after_fail():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10)
    mod.fail(SP, "j1", error="boom")
    assert fake.row()["state"] == "failed"
    mod.tick(SP, "j1", current=4, detail="hop 2")
    r = fake.row()
    assert r["state"] == "running" and r["done"] == 4
    assert r["message"] == "hop 2" and r["error"] is None  # reopen clears
    assert r["total"] == 10                                # absent = keep


def test_tick_self_heals_an_expired_process():
    fake = FakeAny()
    mod = load(fake)
    mod.tick(SP, "j1", current=2, total=8)     # 404 → re-register
    r = fake.row()
    assert r["state"] == "running" and r["done"] == 2 and r["total"] == 8
    assert fake.frames[0][0] == "register"


def test_done_emits_the_terminal_frame_row_lingers():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=3)
    assert mod.done(SP, "j1") == 1
    assert fake.row()["state"] == "done"       # linger view, not a delete
    fake.processes.clear()                     # …after expiry:
    assert mod.done(SP, "j1") == 0             # idempotent on nothing


def test_fail_is_the_terminal_error_frame():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import")
    mod.fail(SP, "j1", error="quota", detail="hop 3")
    r = fake.row()
    assert r["state"] == "failed" and r["error"]["message"] == "quota"
    assert r["message"] == "hop 3"


def test_fail_without_prior_start_materializes_then_fails():
    fake = FakeAny()
    mod = load(fake)
    mod.fail(SP, "j1", error="breaker open")
    r = fake.row()
    assert r["state"] == "failed"
    assert r["error"]["message"] == "breaker open"
    assert [f[0] for f in fake.frames] == ["register", "finish"]


def test_jobs_maps_rows_and_filters_to_this_space():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10, current=4, detail="hop 1")
    mod.fail(SP, "j2", error="boom")
    mod.start("other", "j1", "Elsewhere")      # other space, same job name
    fake.processes["index.embed.x"] = {        # server's own producer
        "identity": "acct1", "self": True, "id": "index.embed.x",
        "kind": "index.embed", "title": "embed", "scope": "device",
        "target": "id-space1", "state": "running", "done": 1,
        "total": None, "message": "", "error": None}
    by_job = {p["job"]: p for p in mod.jobs(SP)}
    assert set(by_job) == {"j1", "j2"}         # ours, this space only
    assert by_job["j1"]["current"] == 4 and by_job["j1"]["total"] == 10
    assert by_job["j1"]["detail"] == "hop 1" and by_job["j1"]["label"] == "Import"
    assert by_job["j2"]["status"] == "failed" and by_job["j2"]["error"] == "boom"


def test_done_with_notify_posts_a_visible_trigger_message_with_counts():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import", total=10)
    mod.tick(SP, "j1", current=10)
    mod.done(SP, "j1", notify={"spaceId": "agentsp", "chatId": "chatX"})
    assert fake.row()["state"] == "done"
    (aspace, chat, body) = fake.chats[-1]
    assert (aspace, chat) == ("agentsp", "chatX")
    # the foreign agent name is what makes the watcher treat it as input
    assert body["agent"]["name"] == "trigger:j1"
    assert "DONE" in body["text"]
    assert "10/10" in body["text"]             # captured before the finish
    assert body["text"].startswith("[trigger: progress]")


def test_fail_with_notify_posts_the_error_and_bare_space_form():
    fake = FakeAny()
    mod = load(fake)
    mod.fail(SP, "j1", error="quota", notify="agentsp")  # chat = general_chat
    (aspace, chat, body) = fake.chats[-1]
    assert (aspace, chat) == ("agentsp", "chat1")
    assert "FAILED: quota" in body["text"]
    assert body["agent"]["name"] == "trigger:j1"


def test_notify_is_best_effort_and_omitted_by_default():
    fake = FakeAny()
    mod = load(fake)
    mod.start(SP, "j1", "Import")
    mod.done(SP, "j1")
    assert fake.chats == []                    # no notify → no chat writes


def test_pre_facility_server_fails_loudly():
    # a server without /v1/processes (pre-#163) is a deployment error,
    # not a compat case (the no-backward-compat rule): the missing
    # route must SURFACE, never silently swallow the bar
    import pytest
    fake = FakeAny(facility=False)
    mod = load(fake)
    with pytest.raises(Exception, match="request.not_found"):
        mod.start(SP, "j1", "Import", total=10)
    with pytest.raises(Exception, match="request.not_found"):
        mod.jobs(SP)
    assert fake.processes == {} and fake.frames == []
