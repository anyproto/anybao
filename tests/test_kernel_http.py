"""The kernel http facade (runtime/guest/app.py): every verb crosses as
`http.<verb>` with the url + kwargs payload and wraps the raw output in
Response. Runs the REAL kernel via kernelenv — the only fake is the
effect boundary."""

import pytest
from kernelenv import load_kernel

VERBS = ["get", "head", "post", "put", "patch", "delete"]


@pytest.mark.parametrize("verb", VERBS)
def test_verb_crosses_as_http_effect(verb):
    seen = []

    def eff(name, payload):
        seen.append((name, payload))
        return {"status": 200, "headers": {"x": "y"},
                "body": '{"ok": true}', "url": "https://api.test/final"}

    app = load_kernel(effect=eff)
    resp = getattr(app.http, verb)("https://api.test/things/1",
                                   json={"k": "v"})

    assert seen == [(f"http.{verb}",
                     {"url": "https://api.test/things/1", "json": {"k": "v"}})]
    assert resp.status == 200
    assert resp.headers == {"x": "y"}
    assert resp.json() == {"ok": True}
    assert resp.url == "https://api.test/final"


# ---- redirects (ADR-008 §2): the facade follows, one effect per hop --------

def _server(routes):
    """An effect fake: `routes[url]` is `(status, location | body)`. The
    reply carries the requested url, as the host's does."""
    seen = []

    def one(name, payload):
        seen.append((name, payload))
        status, rest = routes[payload["url"]]
        if status in (301, 302, 303, 307, 308):
            return {"status": status, "headers": {"location": rest},
                    "body": "", "url": payload["url"]}
        return {"status": status, "headers": {}, "body": rest, "url": payload["url"]}

    def eff(name, payload):
        if name == "batch":
            return {"results": [one(payload["name"], p) for p in payload["payloads"]]}
        return one(name, payload)

    return seen, eff


def test_redirect_is_followed_hop_by_hop_each_an_effect():
    seen, eff = _server({
        "http://a.test/x?q=1": (301, "https://a.test/x"),
        "https://a.test/x": (302, "/y#frag"),
        "https://a.test/y": (200, "done"),
    })
    app = load_kernel(effect=eff)
    resp = app.http.get("http://a.test/x?q=1", headers={"Accept": "text/html"})

    assert (resp.status, resp.text, resp.url) == (200, "done", "https://a.test/y")
    assert seen == [
        ("http.get", {"url": "http://a.test/x?q=1", "headers": {"Accept": "text/html"}}),
        ("http.get", {"url": "https://a.test/x", "headers": {"Accept": "text/html"}}),
        ("http.get", {"url": "https://a.test/y", "headers": {"Accept": "text/html"}}),
    ]


def test_redirects_count_and_manual_mode():
    routes = {f"https://a.test/{i}": (302, f"/{i + 1}") for i in range(9)}
    seen, eff = _server(routes)
    app = load_kernel(effect=eff)

    # manual: the 3xx and its location are the answer; the count does not cross
    resp = app.http.get("https://a.test/0", redirects=0)
    assert (resp.status, resp.headers["location"]) == (302, "/1")
    assert seen == [("http.get", {"url": "https://a.test/0"})]

    del seen[:]
    assert app.http.get("https://a.test/0").url == "https://a.test/5"  # default 5 hops
    assert len(seen) == 6
    del seen[:]
    assert app.http.get("https://a.test/0", redirects=2).url == "https://a.test/2"
    assert len(seen) == 3
    with pytest.raises(TypeError):
        app.http.get("https://a.test/0", redirects=-1)


def test_params_ride_the_first_hop_only():
    seen, eff = _server({"https://a.test/s": (200, "ok")})

    def host(name, payload):  # the host appends params to the url it reports
        if "params" in payload:
            seen.append((name, payload))
            return {"status": 302, "headers": {"location": "/s"}, "body": "",
                    "url": payload["url"] + "?q=1"}
        return eff(name, payload)

    app = load_kernel(effect=host)
    assert app.http.get("https://a.test/go", params={"q": "1"}).text == "ok"
    assert seen[1] == ("http.get", {"url": "https://a.test/s"})


def test_post_redirect_method_and_body_rules():
    seen, eff = _server({
        "https://a.test/form": (303, "/done"),
        "https://a.test/done": (200, "ok"),
        "https://a.test/api": (307, "/api2"),
        "https://a.test/api2": (200, "ok"),
        "https://a.test/out": (308, "https://b.test/in"),
    })
    app = load_kernel(effect=eff)

    # 301/302/303: a bare GET of the new place
    app.http.post("https://a.test/form", json={"k": 1},
                  headers={"Content-Type": "application/json", "X-A": "1"})
    assert seen[1] == ("http.get", {"url": "https://a.test/done", "headers": {"X-A": "1"}})

    # 307/308 inside the origin: the same request again
    del seen[:]
    app.http.post("https://a.test/api", json={"k": 1})
    assert seen[1] == ("http.post", {"url": "https://a.test/api2", "json": {"k": 1}})

    # 307/308 to another origin: a body never leaves the origin
    del seen[:]
    resp = app.http.post("https://a.test/out", body="payload")
    assert resp.status == 308 and len(seen) == 1


def test_cross_origin_hop_drops_origin_bound_headers():
    seen, eff = _server({
        "https://a.test/x": (302, "https://b.test/x"),
        "https://b.test/x": (200, "ok"),
        "https://a.test:8443/x": (200, "ok"),
    })
    app = load_kernel(effect=eff)
    app.http.get("https://a.test/x", headers={
        "Authorization": "Bearer t", "Cookie": "s=1", "Accept": "*/*"})
    assert seen[1] == ("http.get", {"url": "https://b.test/x", "headers": {"Accept": "*/*"}})


def test_credentialed_redirect_is_manual_and_same_origin_only():
    cred = {"ref": "connector.key.x", "header": "X-Api-Token"}
    seen, eff = _server({
        "https://a.test/hop": (302, "/real"),
        "https://a.test/real": (200, "ok"),
        "https://a.test/leak": (302, "https://evil.test/steal"),
        "https://a.test/port": (302, "https://a.test:8443/steal"),
    })
    app = load_kernel(effect=eff)

    # default: manual — the 3xx is data
    assert app.http.get("https://a.test/hop", credential=cred).status == 302
    assert len(seen) == 1

    # an explicit count follows inside the origin; the hop names the
    # credential again, so the host checks its binding again
    del seen[:]
    assert app.http.get("https://a.test/hop", credential=cred, redirects=3).text == "ok"
    assert seen[1] == ("http.get", {"url": "https://a.test/real", "credential": cred})

    # never to another origin — host or port
    for url in ("https://a.test/leak", "https://a.test/port"):
        del seen[:]
        assert app.http.get(url, credential=cred, redirects=3).status == 302
        assert len(seen) == 1


def test_non_http_location_and_missing_location_are_data():
    seen, eff = _server({"https://a.test/f": (302, "file:///etc/passwd")})

    def bare(name, payload):
        if payload["url"].endswith("/none"):
            return {"status": 302, "headers": {}, "body": "", "url": payload["url"]}
        return eff(name, payload)

    app = load_kernel(effect=bare)
    assert app.http.get("https://a.test/f").status == 302
    assert app.http.get("https://a.test/none").status == 302


def test_get_many_follows_per_item_in_batch_rounds():
    seen, eff = _server({
        "https://a.test/1": (200, "one"),
        "https://a.test/2": (302, "/2b"),
        "https://a.test/2b": (302, "/2c"),
        "https://a.test/2c": (200, "two"),
        "https://a.test/3": (302, "/3b"),
        "https://a.test/3b": (200, "three"),
    })
    app = load_kernel(effect=eff)
    out = app.http.get_many([
        "https://a.test/1", "https://a.test/2",
        {"url": "https://a.test/3", "redirects": 0}])

    assert [(r.status, r.text) for r in out] == [(200, "one"), (200, "two"), (302, "")]
    assert out[1].url == "https://a.test/2c"
    assert [n for n, _ in seen] == ["http.get"] * 5  # 3 + 1 + 1, in three crossings
