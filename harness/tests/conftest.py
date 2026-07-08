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
    from anybao.anyclient import AnyClient, http_transport
    return AnyClient(http_transport(any_server))


@pytest.fixture
def fresh_space(client):
    """A throwaway space per test (offline-first create — no coordinator)."""
    import uuid
    sp = client._call("POST", "/v1/spaces",
                      {"name": f"it-{uuid.uuid4().hex[:8]}", "spaceType": "anytype.space"})
    return sp["id"]
