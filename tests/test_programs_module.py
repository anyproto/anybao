"""programs/programs@v1 — the agent-side program write path (ADR-013),
run under the REAL guest kernel (kernelenv): any@v1 dispatches to a
fake in-memory space store, and kernelenv's module_source hook serves
that store to module.resolve — so the post-save use() probe, the
marker-keyed edit liveness, and the kernel import allowlist are all
exercised for real. The validation corpus is shared with deploy.rs
(tests/fixtures/program_validation.jsonl, ADR-013 §3 O2)."""

import json
from pathlib import Path

import pytest
from kernelenv import load_kernel

CORPUS = Path(__file__).parent / "fixtures" / "program_validation.jsonl"
PROGRAMS_DIR = (Path(__file__).resolve().parents[1]
                / "repos" / "_agent" / "programs")

SID = "work12345678901234567890.abc"
OVL = "ovl123456789012345678901.def"

TOOL = '''"""Ping the mailbox and post a line when something matters.

Deliberately tiny — the ADR-013 first-consumer shape."""

__any_tool__ = True


@span("mailWatch.check", kind="getter")
def check(space):
    """One check pass -> {ok}."""
    return {"ok": True}
'''

PLAIN = '''"""A tiny cron job."""


def main(args):
    return {"n": 1}
'''


class FakeAny:
    """The any@v1 surface programs@v1 touches, over an in-memory
    program store: {sid: {oid: {"program": props, "code": str?}}}."""

    def __init__(self, spaces=(SID,)):
        self.spaces = {s: {} for s in spaces}
        self.markers = {}
        self._n = 0

    def _objs(self, sid):
        if sid not in self.spaces:
            raise KeyError(f"unknown space {sid}")
        return self.spaces[sid]

    def seed(self, sid, name, version, code="x = 1\n"):
        """A pre-existing program (e.g. an overlay export)."""
        self._n += 1
        oid = f"obj{self._n}"
        self._objs(sid)[oid] = {
            "program": {"name": name, "version": version,
                        "any_tool": False, "summary": ""},
            "code": code}
        return oid

    # --- the any@v1 methods programs@v1 calls ---
    def get_space(self, sc):
        sid = sc if isinstance(sc, str) else (sc.get("spaceId") or sc.get("id"))
        self._objs(sid)
        return {"id": sid}

    def query_objects(self, sid, filter=None, limit=None):
        f = filter or {}
        out = [{"id": oid, "program": dict(o["program"])}
               for oid, o in self._objs(sid).items()
               if o["program"]["name"] == f.get("program.name")
               and o["program"]["version"] == f.get("program.version")]
        return out[: limit or len(out)]

    def query(self, sid, oid, dataset):
        assert dataset == "program_source"
        o = self._objs(sid)[oid]
        return [{"id": "main", "code": o["code"]}] if "code" in o else []

    def create_object(self, sid, body):
        self._n += 1
        oid = f"obj{self._n}"
        self._objs(sid)[oid] = {
            "program": dict(body["initialProperties"]["program"])}
        return {"objectId": oid}

    def upsert_record(self, sid, oid, dataset, rid, value):
        assert (dataset, rid) == ("program_source", "main")
        self._objs(sid)[oid]["code"] = value["code"]
        self.markers[oid] = self.markers.get(oid, 0) + 1
        return {}

    def update_object(self, sid, oid, body):
        self._objs(sid)[oid]["program"].update(body["program"])
        return {"objectId": oid}

    def delete_object(self, sid, oid):
        del self._objs(sid)[oid]
        return {}

    # --- kernelenv module_source hook: serve "sid:name@vN" ---
    def resolve(self, spec):
        if ":" not in spec:
            return None
        sid, _, rest = spec.partition(":")
        if sid not in self.spaces:
            return None
        name, _, version = rest.rpartition("@")
        for oid, o in self.spaces[sid].items():
            p = o["program"]
            if p["name"] == name and p["version"] == version and "code" in o:
                return {"source": o["code"],
                        "marker": f"m{self.markers.get(oid, 0)}"}
        return None


def kernel(fake, overlays=None):
    def eff(name, payload):
        if name == "config.get":
            if payload["key"] == "overlays.aliases" and overlays is not None:
                return {"value": overlays}
            raise KeyError(f"no config value for {payload['key']!r}")
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=eff, any_client=fake, module_source=fake.resolve)
    return app, app.use("programs@v1")


# --- create ------------------------------------------------------------------

def test_create_tool_is_live_on_use():
    fake = FakeAny()
    app, p = kernel(fake)
    out = p.create_program(SID, {"name": "mailWatch", "source": TOOL})
    assert out == {"ok": True, "objectId": out["objectId"],
                   "spec": "mailWatch@v1", "anyTool": True, "probe": "ok"}
    # deploy's exact storage shape: derived props + program_source/main
    row = fake.spaces[SID][out["objectId"]]
    assert row["program"] == {
        "name": "mailWatch", "version": "v1", "any_tool": True,
        "summary": "Ping the mailbox and post a line when something matters."}
    assert row["code"] == TOOL
    # and the saved tool answers through a real use()
    assert app.use(f"{SID}:mailWatch@v1").check("s") == {"ok": True}


def test_create_non_tool_and_explicit_version():
    fake = FakeAny()
    _, p = kernel(fake)
    out = p.create_program(SID, {"name": "job", "version": "v2",
                                 "source": PLAIN})
    assert out["ok"] and out["spec"] == "job@v2" and out["anyTool"] is False
    assert fake.spaces[SID][out["objectId"]]["program"]["any_tool"] is False


def test_create_refusals_write_nothing():
    fake = FakeAny()
    _, p = kernel(fake)
    cases = [
        ({"name": "x", "source": '"""Doc."""\ndef f(:\n'}, "SyntaxError"),
        ({"name": "mail-watch", "source": PLAIN}, "identifier"),
        ({"name": "x", "version": "1", "source": PLAIN}, "v<N>"),
        ({"name": "x", "source": PLAIN, "extra": 1}, "unknown key"),
        ({"name": "x", "source": ""}, "non-empty"),
    ]
    for body, err in cases:
        with pytest.raises((ValueError, TypeError), match=err):
            p.create_program(SID, body)
    assert fake.spaces[SID] == {}


def test_create_duplicate_spec_refused():
    fake = FakeAny()
    _, p = kernel(fake)
    p.create_program(SID, {"name": "job", "source": PLAIN})
    with pytest.raises(ValueError, match="already exists"):
        p.create_program(SID, {"name": "job", "source": PLAIN})


def test_probe_failure_is_saved_but_not_a_tool():
    # passes the static gate (valid syntax, allowed imports) but dies
    # at module exec — the probe catches it; the source stays saved
    fake = FakeAny()
    _, p = kernel(fake)
    broken = TOOL + "\nX = boom_undefined\n"
    out = p.create_program(SID, {"name": "mailWatch", "source": broken})
    assert out["ok"] is False and out["saved"] is True
    assert out["anyTool"] is False
    assert "boom_undefined" in out["probe"]
    assert "edit_program" in out["hint"]
    row = fake.spaces[SID][out["objectId"]]
    assert row["code"] == broken                      # recoverable state
    assert row["program"]["any_tool"] is False        # never half-bound


# --- shadow guard (ADR-013 §1) -----------------------------------------------

def test_overlay_exported_spec_refused():
    fake = FakeAny(spaces=(SID, OVL))
    fake.seed(OVL, "any", "v1")
    _, p = kernel(fake, overlays={"agent": OVL})
    with pytest.raises(ValueError, match="`agent` overlay"):
        p.create_program(SID, {"name": "any", "source": PLAIN})
    # a different version of the same name is not the exported spec
    out = p.create_program(SID, {"name": "any", "version": "v9",
                                 "source": PLAIN})
    assert out["ok"]


def test_alias_bound_to_working_space_is_skipped():
    # degenerate single-space shape: agent alias == working space —
    # the guard must not match the program being written/updated
    fake = FakeAny()
    _, p = kernel(fake, overlays={"agent": SID})
    out = p.create_program(SID, {"name": "job", "source": PLAIN})
    assert out["ok"]
    assert p.update_program(SID, "job@v1", PLAIN)["ok"]


def test_no_overlay_config_means_no_guard():
    fake = FakeAny()
    _, p = kernel(fake, overlays=None)   # config.get errors -> {}
    assert p.create_program(SID, {"name": "job", "source": PLAIN})["ok"]


# --- update / edit -----------------------------------------------------------

def test_update_is_live_on_next_use():
    fake = FakeAny()
    app, p = kernel(fake)
    p.create_program(SID, {"name": "job", "source": PLAIN})
    assert app.use(f"{SID}:job@v1").main(None) == {"n": 1}
    out = p.update_program(SID, "job@v1", PLAIN.replace('{"n": 1}', '{"n": 2}'))
    assert out["ok"]
    # marker bumped -> fresh module, no stale cache (ADR-004 §4)
    assert app.use(f"{SID}:job@v1").main(None) == {"n": 2}


def test_update_missing_spec_refused():
    fake = FakeAny()
    _, p = kernel(fake)
    with pytest.raises(ValueError, match="not found"):
        p.update_program(SID, "ghost@v1", PLAIN)


def test_edit_applies_and_is_live():
    fake = FakeAny()
    app, p = kernel(fake)
    p.create_program(SID, {"name": "job", "source": PLAIN})
    out = p.edit_program(SID, "job@v1",
                         [{"oldText": '"n": 1', "newText": '"n": 3'}])
    assert out["ok"]
    assert app.use(f"{SID}:job@v1").main(None) == {"n": 3}


def test_edit_replace_all_and_ambiguity():
    src = '"""Doc."""\n\nA = "zig"\nB = "zig"\n\n\ndef main(args):\n    return A + B\n'
    fake = FakeAny()
    app, p = kernel(fake)
    p.create_program(SID, {"name": "job", "source": src})
    with pytest.raises(ValueError, match="matches 2 places"):
        p.edit_program(SID, "job@v1",
                       [{"oldText": '"zig"', "newText": '"zag"'}])
    out = p.edit_program(SID, "job@v1",
                         [{"oldText": '"zig"', "newText": '"zag"',
                           "replaceAll": True}])
    assert out["ok"]
    assert app.use(f"{SID}:job@v1").main(None) == "zagzag"


def test_edit_refusals_keep_the_old_source():
    fake = FakeAny()
    _, p = kernel(fake)
    oid = p.create_program(SID, {"name": "job", "source": PLAIN})["objectId"]
    cases = [
        ([{"oldText": "nowhere-to-be-found", "newText": "x"}], "not found"),
        ([{"oldText": "def main", "newText": "def maint"}], "drops main"),
        ([{"oldText": '"""A tiny cron job."""', "newText": ""}],
         "no module docstring"),
        ([{"bad": "shape"}], "oldText"),
        ([], "non-empty"),
    ]
    for edits, err in cases:
        with pytest.raises(ValueError, match=err):
            p.edit_program(SID, "job@v1", edits)
    assert fake.spaces[SID][oid]["code"] == PLAIN     # all-or-nothing


# --- delete ------------------------------------------------------------------

def test_delete_program():
    fake = FakeAny()
    _, p = kernel(fake)
    oid = p.create_program(SID, {"name": "job", "source": PLAIN})["objectId"]
    assert p.delete_program(SID, "job@v1") == {
        "ok": True, "objectId": oid, "spec": "job@v1"}
    assert fake.spaces[SID] == {}
    with pytest.raises(ValueError, match="not found"):
        p.delete_program(SID, "job@v1")


# --- validation parity corpus (ADR-013 §3 O2, shared with deploy.rs) --------

def test_validation_parity_corpus():
    _, p = kernel(FakeAny())
    n = 0
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        f = json.loads(line)
        if f["guest"]["ok"]:
            p._validate_source("t@v1", f["code"])      # must not raise
        else:
            with pytest.raises(ValueError) as ei:
                p._validate_source("t@v1", f["code"])
            assert f["guest"].get("err", "") in str(ei.value), f["name"]
        n += 1
    assert n >= 10, f"corpus suspiciously small ({n} fixtures)"


def test_programs_v1_passes_its_own_gate():
    # dogfood: the write path's own source satisfies the gate it enforces
    src = (PROGRAMS_DIR / "programs@v1" / "program.py").read_text()
    _, p = kernel(FakeAny())
    p._validate_source("programs@v1", src)
