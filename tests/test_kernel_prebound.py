"""datetime / tempfile are bound in every cell, and importing them stays legal."""

from kernelenv import load_kernel


def _host(name, payload):
    if name == "runtime.get":
        return {"value": None}
    if name == "blob.put":
        return {"__blob": "sha256:" + "cd" * 32, "bytes": 1, "mime": payload["mime"]}
    if name in ("clock.now", "time.now"):
        return {"now": 1700000000.0}
    return {}


def test_datetime_and_tempfile_work_unimported_and_imported():
    app = load_kernel(effect=_host)
    res = app._run_cell("with tempfile.TemporaryFile(mime='x/y') as w:\n"
                        "    w.write(b'z')\nzb = w.blob\nd = datetime.timedelta(days=1)\n", "c1")
    assert res["ok"], res
    assert app._ns["d"].days == 1
    res = app._run_cell("import datetime, tempfile\nimport datetime as dt\n", "c2")
    assert res["ok"], res
    assert app._ns["dt"] is app._ns["datetime"]
