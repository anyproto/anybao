import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# Integration tests hit a REAL any server. They validate the WIRE
# CONTRACT the offline fake-transport tests can't (two real bugs in one
# session — seq-retry-vs-validation and reserved-group relabeling — both
# slipped past fakes). Marked `integration`; skipped unless a server is
# reachable at ANYBAO_TEST_SERVER (default 127.0.0.1:7009). Run offline
# suite = default; run integration = `-m integration`.
DEFAULT_SERVER = "http://127.0.0.1:7009"


def _server_url() -> str:
    import os
    return os.environ.get("ANYBAO_TEST_SERVER", DEFAULT_SERVER)


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/v1/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def any_server() -> str:
    url = _server_url()
    if not _reachable(url):
        pytest.skip(f"no any server at {url} (set ANYBAO_TEST_SERVER or `any run --addr`)")
    return url


@pytest.fixture
def client(any_server):
    from anybao.anyclient import AnyClient, http_transport, sse_http_transport
    return AnyClient(http_transport(any_server), sse_http_transport(any_server))


@pytest.fixture
def fresh_space(client):
    """A throwaway space per test (offline-first create — no coordinator)."""
    import uuid
    sp = client._call("POST", "/v1/spaces",
                      {"name": f"it-{uuid.uuid4().hex[:8]}", "spaceType": "anytype.space"})
    return sp["id"]


@pytest.fixture
def guest_use(any_server):
    """Guest modules exec'd host-side with a REAL-http effect shim — the
    integration twin of the offline exec-with-fakes technique. Returns a
    use() that loads from programs/ and hits the live server."""
    import json as _json  # noqa: F401 - parity with the guest env
    import time as _time
    import types

    from anybao.effects_impl import _http_request

    programs = Path(__file__).resolve().parents[2] / "programs"
    cache: dict = {}

    def eff(name, payload=None):
        payload = payload or {}
        if name.startswith("http."):
            return _http_request(
                name.split(".")[1].upper(), payload["url"],
                params=payload.get("params"), headers=payload.get("headers"),
                json_body=payload.get("json"), body=payload.get("body"),
                timeout=payload.get("timeout"))
        if name == "config.get":
            return {"value": {"any.base_url": any_server}[payload["key"]]}
        raise AssertionError(f"unexpected effect in guest shim: {name}")

    def use(spec):
        if spec not in cache:
            g = {"effect": eff, "span": lambda n: (lambda f: f), "use": use,
                 "now": lambda: int(_time.time())}
            src = (programs / f"{spec}.py").read_text()
            exec(compile(src, f"{spec}.py", "exec"), g)
            mod = types.SimpleNamespace()
            mod.__dict__.update(g)
            cache[spec] = mod
        return cache[spec]

    return use
