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
