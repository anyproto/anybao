import pytest
from anybao.anyclient import AnyClient, AnyError, sanitize_nuls


def fake_transport(status=200, data=None, capture=None):
    def send(method, path, body):
        if capture is not None:
            capture.append((method, path, body))
        return status, (data or {})
    return send


def test_nul_sanitized_on_write():
    cap = []
    c = AnyClient(fake_transport(data={"records": []}, capture=cap))
    c.modify("s1", {"body": "before\x00after", "nested": {"x": ["a\x00b"]}})
    _, _, sent = cap[0]
    assert "\x00" not in sent["body"]
    assert "\x00" not in sent["nested"]["x"][0]


def test_sanitize_leaves_clean_strings_identical():
    obj = {"a": "clean", "b": [1, 2]}
    assert sanitize_nuls(obj) == obj


def test_error_mapping():
    c = AnyClient(fake_transport(
        status=404, data={"error": {"code": "space.not_found", "message": "no"}}))
    with pytest.raises(AnyError) as ei:
        c.query("s", "o", "d")
    assert ei.value.status == 404 and ei.value.code == "space.not_found"


def test_query_returns_records():
    c = AnyClient(fake_transport(data={"records": [{"id": "1"}]}))
    assert c.query("s", "o", "prop") == [{"id": "1"}]
