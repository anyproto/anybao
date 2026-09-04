"""The kernel shell facades (runtime/guest/app.py, ADR-024 §4): `sh` /
`fs` exist in the cell namespace iff `runtime.get("shell")` resolves
(probed once per kernel), `sh(cmd)` crosses as `sh.run` and wraps the
result (exit/timeout are data; `check=True` raises), `fs.*` cross as
the four fs effects. Runs the REAL kernel via kernelenv — the only
fake is the effect boundary."""

from kernelenv import load_kernel

INFO = {"cwd": "/home/u/proj", "home": "/home/u", "shell": "/bin/zsh", "os": "linux"}


def _run_ok(cmd_out="", err="", code=0, **extra):
    raw = {"pid": 42, "exit": code, "stdout": cmd_out, "stderr": err,
           "durationMs": 3, "truncated": False, "timedOut": False}
    raw.update(extra)
    return raw


def test_without_the_feature_names_are_absent_and_probe_is_once():
    probes = []

    def eff(name, payload):
        if name == "runtime.get":
            probes.append(payload)
            raise KeyError("no runtime value for \"shell\"")
        raise AssertionError(name)

    app = load_kernel(effect=eff)
    out = app._run_cell("x = 1", "c1")
    assert out["ok"]
    assert "sh" not in app._ns and "fs" not in app._ns
    out = app._run_cell("sh", "c2")
    assert out["error"]["type"] == "NameError"
    assert probes == [{"key": "shell"}]  # once per kernel, not per cell


def test_with_the_feature_sh_and_fs_are_bound_and_documented():
    app = load_kernel(shell=INFO)
    out = app._run_cell("print(sh.cwd, sh.os); help(sh); help(fs.edit)", "c1")
    assert out["ok"], out["error"]
    assert out["prints"][0]["repr"] == "/home/u/proj linux"
    assert "sh(\"cmd\"" in out["prints"][1]["repr"]
    assert "run(cmd, **kw)" in out["prints"][1]["repr"]
    assert "edit(path, old, new, all=False)" in out["prints"][2]["repr"]


def test_sh_call_crosses_as_sh_run_and_wraps_result():
    seen = []

    def eff(name, payload):
        seen.append((name, payload))
        return _run_ok("a\nb\n", err="warn\n", code=0)

    app = load_kernel(effect=eff, shell=INFO)
    out = app._run_cell(
        'r = sh("ls -1", cwd="/tmp", timeout_s=5)\n'
        'print(r.ok, r.code, r.lines(), r.err, r.pid, r.timed_out)\n'
        'print(sh.last is r)\n'
        'r', "c1")
    assert out["ok"], out["error"]
    assert seen == [("sh.run", {"cmd": "ls -1", "cwd": "/tmp", "timeout_s": 5})]
    assert out["prints"][0]["repr"] == "True 0 ['a', 'b'] warn\n 42 False"
    assert out["prints"][1]["repr"] == "True"
    # the last-expression repr reads like a terminal: stdout, then stderr
    assert out["last"]["repr"] == "a\nb\n[stderr]\nwarn"


def test_sh_nonzero_timeout_and_check():
    calls = iter([
        _run_ok("", err="boom\n", code=2),
        _run_ok("partial\n", code=None, timedOut=True, durationMs=1200),
        _run_ok("", code=1),
        _run_ok("x\n", code=0, truncated=True),
    ])
    app = load_kernel(effect=lambda n, p: next(calls), shell=INFO)
    out = app._run_cell(
        'a = sh("false")\nb = sh("sleep 9", timeout_s=1)\n'
        'try:\n    sh("false", check=True)\n    c = "no raise"\n'
        'except ShellError as e:\n    c = f"raised {e.result.code}"\n'
        'print(repr(a)); print(repr(b)); print(c); print(repr(sh("big")))', "c1")
    assert out["ok"], out["error"]
    reprs = [p["repr"] for p in out["prints"]]
    assert reprs[0] == "[stderr]\nboom\n[exit 2]"
    assert reprs[1] == "partial\n[timed out after 1200 ms — partial output above]"
    assert reprs[2] == "raised 1"
    assert reprs[3] == "x\n[output over the capture cap — head and tail kept]"


def test_sh_lines_and_raw_run():
    app = load_kernel(effect=lambda n, p: _run_ok("1\n2\n"), shell=INFO)
    out = app._run_cell('print(sh.lines("seq 2")); print(sh.run("seq 2")["exit"])', "c1")
    assert out["ok"], out["error"]
    assert out["prints"][0]["repr"] == "['1', '2']"
    assert out["prints"][1]["repr"] == "0"


def test_fs_facade_crosses_as_fs_effects():
    seen = []

    def eff(name, payload):
        seen.append((name, payload))
        if payload.get("encoding") == "blob":
            return {"path": payload["path"], "size": 4,
                    "blob": {"__blob": "sha256:" + "ab" * 32, "bytes": 4, "mime": payload["mime"]}}
        return {
            "fs.read": {"path": payload["path"], "text": "l2\nl3\n", "size": 12, "lines": 4,
                        "offset": payload.get("offset", 1), "truncated": False},
            "fs.write": {"path": payload["path"], "bytes": 3, "created": True},
            "fs.edit": {"path": payload["path"], "replacements": 1},
            "fs.list": {"path": payload["path"], "truncated": False,
                        "entries": [{"path": "/p/a.rs", "kind": "file", "size": 1}]},
        }[name]

    app = load_kernel(effect=eff, shell=INFO)
    out = app._run_cell(
        't = fs.read("/p/f", offset=2, limit=2)\n'
        'print(t, t.lines, t.size, t.truncated, isinstance(t, str))\n'
        'print(fs.write("/p/n", "abc"))\n'
        'print(fs.edit("/p/f", "l2", "L2"))\n'
        'print(fs.list("/p", glob="*.rs", depth=2))\n'
        'b = fs.read("/p/b.png", encoding="blob")\n'
        'print(isinstance(b, blob.Blob), b.mime, b.size)\n'
        'print(fs.write("/p/o.png", b))', "c1")
    assert out["ok"], out["error"]
    reprs = [p["repr"] for p in out["prints"]]
    assert reprs[0] == "l2\nl3\n 4 12 False True"
    assert reprs[1] == "{'path': '/p/n', 'bytes': 3, 'created': True}"
    assert reprs[2] == "1"
    assert reprs[3] == "[{'path': '/p/a.rs', 'kind': 'file', 'size': 1}]"
    assert reprs[4] == "True image/png 4"  # the Blob, mime from the name
    assert seen[:4] == [
        ("fs.read", {"path": "/p/f", "offset": 2, "limit": 2}),
        ("fs.write", {"path": "/p/n", "content": "abc", "mkdirs": True}),
        ("fs.edit", {"path": "/p/f", "old": "l2", "new": "L2", "all": False}),
        ("fs.list", {"path": "/p", "depth": 2, "glob": "*.rs"}),
    ]
    # ADR-026 §4 on fs: the byte legs carry the ref, never the bytes
    ref = {"__blob": "sha256:" + "ab" * 32, "bytes": 4, "mime": "image/png"}
    assert seen[4] == ("fs.read", {"path": "/p/b.png", "encoding": "blob", "mime": "image/png"})
    assert seen[5] == ("fs.write", {"path": "/p/o.png", "content": ref, "mkdirs": True})


def test_fs_write_wraps_bytes_into_a_blob_first():
    """`fs.write(path, b"…")` = one `blob.put` (the bytes go to the
    directory once) then `fs.write` with the ref; a mime override and an
    unknown extension on the read leg."""
    seen = []

    def eff(name, payload):
        seen.append((name, payload))
        if name == "blob.put":
            return {"__blob": "sha256:" + "cd" * 32, "bytes": 4, "mime": payload["mime"]}
        if name == "fs.write":
            return {"path": payload["path"], "bytes": 4, "created": True}
        if name == "fs.read":
            return {"path": payload["path"], "size": 9,
                    "blob": {"__blob": "sha256:" + "ef" * 32, "bytes": 9, "mime": payload["mime"]}}
        raise AssertionError(name)

    app = load_kernel(effect=eff, shell=INFO)
    out = app._run_cell(
        'print(fs.write("/p/o.bin", b"\\x00\\x9f\\x92\\x96")["bytes"])\n'
        'print(fs.read("/p/x.weird", encoding="blob").mime)\n'
        'print(fs.read("/p/x.weird", encoding="blob", mime="application/pdf").mime)', "c1")
    assert out["ok"], out["error"]
    octet = "application/octet-stream"
    assert [p["repr"] for p in out["prints"]] == ["4", octet, "application/pdf"]
    assert seen[0] == ("blob.put", {"data": "AJ+Slg==", "mime": octet})
    assert seen[1] == ("fs.write", {"path": "/p/o.bin", "mkdirs": True, "content":
                       {"__blob": "sha256:" + "cd" * 32, "bytes": 4, "mime": octet}})
    assert seen[2][1]["mime"] == "application/octet-stream"
    assert seen[3][1]["mime"] == "application/pdf"
    err = app._run_cell('fs.write("/p/o", 42)', "c2")["error"]
    assert err["type"] == "TypeError"


def test_use_modules_do_not_carry_sh():
    """Programs reach the shell through cells (the toolcaller's bash tool
    is a subcell) — no probe, no binding at module load."""
    probes = []

    def eff(name, payload):
        probes.append(name)
        raise AssertionError(name)

    app = load_kernel(effect=eff, shell=INFO, module_source=lambda s: "X = 1\n")
    mod = app.use("thing@v1")
    assert mod.X == 1 and "sh" not in mod.__dict__
    assert probes == []


def test_bash_tool_cell_code_runs_and_binds_in_the_kernel():
    """The exact cell the toolcaller's bash tool emits (`_bash_code`):
    the result is the cell's last value (what the toolcaller renders
    via values.get) and lands as both `sh.last` and the `as` name."""
    app = load_kernel(effect=lambda n, p: _run_ok("ok\n", code=0), shell=INFO)
    out = app._run_cell("tests = sh('cargo test', cwd='/p', timeout_s=30)\ntests", "b1")
    assert out["ok"], out["error"]
    res = app.values.get("b1", "last")
    assert res.out == "ok\n" and res.code == 0 and res.cmd == "cargo test"
    out = app._run_cell("print(tests is sh.last, tests is values.get('b1'))", "c2")
    assert out["prints"][0]["repr"] == "True True"
