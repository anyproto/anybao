"""The http syscall — route classification, credential injection."""

import contextlib

import pytest
from anybao.effects_impl import register_http_effects
from anybao.routes import Classifier
from anyrt import trace as tr
from anyrt.effects import Broker, Registry

ANY = "http://127.0.0.1:7011"


def broker(secrets=None, classifier=None):
    seen = []

    def fake_request(method, url, *, params=None, headers=None,
                     json_body=None, body=None, timeout=None):
        seen.append({"method": method, "url": url, "headers": headers or {},
                     "json": json_body})
        return {"status": 200, "headers": {}, "body": "{}"}

    reg = Registry()
    register_http_effects(reg, secrets=secrets,
                          classifier=classifier or Classifier(any_base=ANY),
                          request=fake_request)
    w = tr.TraceWriter(run={"id": "r"})
    return Broker(reg, w), w, seen


CASES = [
    ("http.get", {"url": f"{ANY}/v1/spaces/s1/types"}, "read", "data.read"),
    ("http.post", {"url": f"{ANY}/v1/spaces/s1/query"}, "read", "data.read"),
    ("http.post", {"url": f"{ANY}/v1/spaces/s1/search"}, "read", "data.read"),
    ("http.post", {"url": f"{ANY}/v1/spaces/s1/modify"}, "mutate", "data.write"),
    ("http.post", {"url": f"{ANY}/v1/spaces/s1/objects"}, "mutate", "data.write"),
    ("http.post", {"url": "https://api.anthropic.com/v1/messages"}, "read", "llm.chat"),
    ("http.post", {"url": "https://api.openai.com/v1/chat/completions"}, "read", "llm.chat"),
    ("http.get", {"url": "https://example.com/page"}, "read", "net.http"),
    ("http.post", {"url": "https://example.com/api/query"}, "mutate", "net.http"),
    ("http.delete", {"url": f"{ANY}/v1/spaces/s1/objects/o1"}, "mutate", "data.write"),
]


@pytest.mark.parametrize("name,payload,kind,cap", CASES)
def test_route_classification_lands_in_trace_and_caps(name, payload, kind, cap):
    b, w, _ = broker()
    b.call(name, payload)
    rec = next(r for r in w.records if r["kind"] == "effect")
    assert rec["meta"]["class"] == kind

    class Deny:
        def __init__(self):
            self.asked = []

        def allowed(self, c):
            self.asked.append(c)
            return False

    deny = Deny()
    b2, w2, _ = broker()
    b2.grants = deny
    with contextlib.suppress(Exception):
        b2.call(name, payload)
    assert deny.asked == [cap]


def test_base_scoping_keeps_foreign_lookalike_paths_out_of_data_caps():
    # with any_base set, an /v1/spaces path on a FOREIGN host is not data
    b, w, _ = broker()
    b.call("http.post", {"url": "https://elsewhere.io/v1/spaces/s1/query"})
    rec = next(r for r in w.records if r["kind"] == "effect")
    assert rec["meta"]["class"] == "mutate"
    # heuristic fallback (no base): the path shape decides
    b2, w2, _ = broker(classifier=Classifier())
    b2.call("http.post", {"url": "https://elsewhere.io/v1/spaces/s1/query"})
    rec2 = next(r for r in w2.records if r["kind"] == "effect")
    assert rec2["meta"]["class"] == "read"


def test_credential_injected_after_recording_never_in_trace():
    b, w, seen = broker(secrets={"llm.key": "sk-SECRET"}.get)
    b.call("http.post", {
        "url": "https://api.anthropic.com/v1/messages",
        "json": {"model": "m"},
        "headers": {"anthropic-version": "2023-06-01"},
        "credential": {"ref": "llm.key", "header": "x-api-key"}})
    assert seen[0]["headers"]["x-api-key"] == "sk-SECRET"
    assert seen[0]["headers"]["anthropic-version"] == "2023-06-01"
    rec = next(r for r in w.records if r["kind"] == "effect")
    assert "sk-SECRET" not in str(rec)                       # value never recorded
    assert rec["input"]["credential"] == {"ref": "llm.key", "header": "x-api-key"}


def test_credential_prefix_builds_bearer_headers():
    b, _, seen = broker(secrets={"k": "tok"}.get)
    b.call("http.post", {"url": "https://api.openai.com/v1/chat/completions",
                         "credential": {"ref": "k", "header": "Authorization",
                                        "prefix": "Bearer "}})
    assert seen[0]["headers"]["Authorization"] == "Bearer tok"


def test_credential_without_resolver_is_loud():
    b, w, _ = broker(secrets=None)
    with pytest.raises(Exception, match="no secrets resolver"):
        b.call("http.get", {"url": "https://x.io",
                            "credential": {"ref": "k", "header": "h"}})
