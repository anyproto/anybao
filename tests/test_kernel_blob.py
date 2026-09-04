"""Blobs in the guest (runtime/guest/app.py, ADR-026 §4): a Response
whose body the host classified as bytes carries a Blob; `bytes(b)` /
`read(n)` cross as `blob.read`, `blob.from_bytes` and a `tempfile`
writer as `blob.put`; a Blob rides effect payloads and span inputs as
its ref. Runs the REAL kernel via kernelenv — the only fake is the
effect boundary."""

import base64

import pytest
from kernelenv import load_kernel

REF = {"__blob": "sha256:" + "ab" * 32, "bytes": 10, "mime": "image/png"}
PAYLOAD = b"\x89PNG-data!"


def _host(seen, store=None):
    store = store if store is not None else {REF["__blob"]: PAYLOAD}

    def eff(name, payload):
        seen.append((name, payload))
        if name == "runtime.get":
            return {"value": None}
        if name == "http.get":
            return {"status": 200, "headers": {"content-type": "image/png"},
                    "body": dict(REF), "url": payload["url"]}
        if name == "http.post":
            return {"status": 201, "headers": {}, "body": "{}", "url": payload["url"]}
        if name == "blob.read":
            data = store[payload["hash"]][payload["offset"]:payload["offset"] + payload["length"]]
            return {"data": base64.b64encode(data).decode(), "bytes": len(data)}
        if name == "blob.put":
            raw = base64.b64decode(payload["data"])
            h = "sha256:" + "cd" * 32
            store[h] = raw
            return {"__blob": h, "bytes": len(raw), "mime": payload["mime"]}
        raise AssertionError(name)
    return eff


def test_binary_response_carries_a_blob_and_text_raises():
    seen = []
    app = load_kernel(effect=_host(seen))
    r = app.http.get("https://x/img.png")
    assert isinstance(r.blob, app.Blob)
    assert (r.blob.mime, r.blob.size, r.blob.sha256) == ("image/png", 10, REF["__blob"])
    with pytest.raises(app.BinaryBody):
        _ = r.text
    with pytest.raises(app.BinaryBody):
        r.json()
    assert repr(r.blob).startswith("<Blob image/png 10 bytes sha256:")
    assert len(r.blob) == 10


def test_bytes_and_ranged_reads_cross_as_blob_read():
    seen = []
    app = load_kernel(effect=_host(seen))
    b = app.http.get("https://x/img.png").blob
    assert b.read(4) == PAYLOAD[:4]
    assert b.tell() == 4
    assert b.read() == PAYLOAD[4:]
    b.seek(0)
    assert bytes(b) == PAYLOAD
    assert b.text(errors="replace").endswith("PNG-data!")
    reads = [p for n, p in seen if n == "blob.read"]
    assert reads[0] == {"hash": REF["__blob"], "offset": 0, "length": 4}
    assert reads[1] == {"hash": REF["__blob"], "offset": 4, "length": 6}
    assert reads[2] == {"hash": REF["__blob"], "offset": 0, "length": 10}


def test_bytes_over_the_ceiling_is_refused_with_a_hint():
    app = load_kernel(effect=_host([]))
    big = app.Blob("sha256:" + "ef" * 32, 65 * 1024 * 1024, "application/zip")
    with pytest.raises(ValueError, match="read it in ranges"):
        bytes(big)


def test_from_bytes_and_tempfile_cross_as_blob_put():
    seen = []
    app = load_kernel(effect=_host(seen))
    b = app.blob.from_bytes(b"zip!", "application/zip")
    assert (b.mime, b.size) == ("application/zip", 4)
    assert seen[-1] == ("blob.put", {"data": base64.b64encode(b"zip!").decode(),
                                     "mime": "application/zip"})
    # a temporary file is the writer: bytes in, a Blob on close
    ns = {}
    app._run_cell(
        "import tempfile\n"
        "with tempfile.TemporaryFile(mime='text/csv') as w:\n"
        "    w.write(b'a,b\\n')\n"
        "    w.write(b'1,2\\n')\n"
        "out = w.blob\n", "c1")
    out = app._ns["out"]
    assert isinstance(out, app.Blob) and out.mime == "text/csv" and out.size == 8
    assert seen[-1][0] == "blob.put"
    assert base64.b64decode(seen[-1][1]["data"]) == b"a,b\n1,2\n"
    # text mode encodes utf-8; directories are refused with the pointer
    app._run_cell("import tempfile\nw = tempfile.NamedTemporaryFile('w+')\n"
                  "w.write('héllo')\nw.close()\nt = w.blob\n", "c2")
    assert app._ns["t"].size == len("héllo".encode())
    res = app._run_cell("import tempfile\ntempfile.mkdtemp()\n", "c3")
    assert res["ok"] is False and "ADR-024" in res["error"]["message"]
    del ns


def test_a_blob_rides_payloads_and_span_inputs_as_its_ref():
    seen = []
    app = load_kernel(effect=_host(seen))
    b = app.http.get("https://x/img.png").blob
    app.http.post("https://any/attach", body=b, json=None)
    posted = [p for n, p in seen if n == "http.post"][0]
    assert posted["body"] == REF
    app.http.post("https://llm/chat", json={"parts": [{"data": b}]})
    posted = [p for n, p in seen if n == "http.post"][1]
    assert posted["json"]["parts"][0]["data"] == REF
    assert app._json_safe({"b": b}) == {"b": REF}
    assert app.blob.of(REF) == b and app.blob.is_ref(REF)
