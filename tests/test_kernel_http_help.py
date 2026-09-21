"""`help(http.get)` names the return type, the keywords and the
recorded wire shape (BOB-87 / BOB-121 G3): a model that asks must not
have to guess `status_code`/`text` for a mock record."""

from kernelenv import load_kernel


def test_help_on_http_verbs_states_response_keywords_and_wire_shape():
    app = load_kernel(effect=lambda n, p: {})
    for verb in ("get", "post", "put", "patch", "delete", "head"):
        text = app.describe(getattr(app.http, verb))
        assert "Response" in text and ".status" in text and ".json()" in text, (verb, text)
        assert "credential" in text and "params" in text and "timeout" in text, (verb, text)
        # streaming is discoverable from help() (ADR-002 §1, BOB-149)
        assert "stream=True" in text and '"idle": 60, "total": 900' in text, (verb, text)
        assert f"`http.{verb}`" in text and "url, body}" in text, (verb, text)
        assert "not `status_code`" in text, (verb, text)
    # get_many points at the same shape
    assert "help(http.get)" in app.describe(app.http.get_many)
