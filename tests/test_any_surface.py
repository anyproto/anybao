"""any@v1's flat surface is GENERATED from the @_public-marked _Client
methods (ADR-010 §8). Under the real kernel span machinery: every marked
method is exported with its doc, the span input keeps the parameter-keyed
shape recordings and mocks match on, unlisted plumbing stays callable
but out of describe(), and a bad call names the function."""

import contextlib
import json

import pytest
from kernelenv import load_kernel

SID = "bafyreibij6sy5ruc5qobzx3bbfznrsttptcd6rtxwb3i4sqebx3juopor4.28llrna4aa8p0"


def _kernel():
    spans = []

    def effect(name, payload):
        if name == "runtime.get":
            if payload["key"] == "any.base_url":
                return {"value": "http://any"}
            raise KeyError(payload["key"])
        if name.startswith("http."):
            return {"status": 200, "headers": {}, "body": json.dumps({})}
        raise AssertionError(f"unexpected effect {name}")

    app = load_kernel(effect=effect,
                      on_span=lambda n, p: spans.append(p) if n == "span.begin" else None)
    return app, app.use("any@v1"), spans


def _begin(mod, spans, fn, *args, **kwargs):
    spans.clear()
    with contextlib.suppress(Exception):   # the empty fake wire; only the span input matters
        getattr(mod, fn)(*args, **kwargs)
    return spans[0]


def test_every_public_method_is_exported_with_its_doc():
    _, mod, _ = _kernel()
    marked = [n for n, m in mod._Client.__dict__.items() if hasattr(m, "__any_public__")]
    assert len(marked) >= 60
    for n in marked:
        fn = getattr(mod, n)
        assert fn.__doc__ and fn.__doc__ == getattr(mod._Client, n).__doc__, n
        assert fn.__span_kind__ == mod._Client.__dict__[n].__any_public__[0], n


def test_span_input_is_parameter_keyed_like_the_old_wrappers():
    _, mod, spans = _kernel()
    b = _begin(mod, spans, "create_object", SID, {"name": "x"})
    assert (b["name"], b["kind"]) == ("any.create_object", "mutator")
    assert b["input"] == {"spaceConfig": SID, "body": {"name": "x"}}
    b = _begin(mod, spans, "query_objects", SID, filter={"any.type": "page"}, limit=2)
    assert b["input"] == {"spaceConfig": SID, "filter": {"any.type": "page"}, "limit": 2}
    b = _begin(mod, spans, "list_spaces")
    assert (b["name"], b["kind"], b["input"]) == ("any.list_spaces", "getter", {})
    b = _begin(mod, spans, "get_markdown", spaceConfig=SID, object_id="o1")
    assert b["input"] == {"spaceConfig": SID, "object_id": "o1"}


def test_signature_renders_spaceconfig_first_without_self():
    app, mod, _ = _kernel()
    assert app.describe(mod.create_object).startswith(
        "create_object(spaceConfig, body, create_options=True, parent=None, folder=None) [mutator]")
    assert app.describe(mod.list_spaces).startswith("list_spaces(raw=False) [getter]")


def test_unlisted_plumbing_is_callable_but_out_of_listings():
    app, mod, _ = _kernel()
    listing = app.describe(mod)
    for n in ("append_turn", "create_chunk", "create_memory", "ensure_bundle", "modify"):
        assert f"\n  {n}(" not in listing, n
        assert callable(getattr(mod, n)) and getattr(mod, n).__doc__
        assert app.describe(getattr(mod, n)).startswith(n + "(")
    assert "\n  create_object(" in listing and "\n  chat_log(" in listing


def test_a_bad_call_names_the_function():
    _, mod, _ = _kernel()
    with pytest.raises(TypeError, match=r"create_object\(\) missing a required argument: 'body'"):
        mod.create_object(SID)
    with pytest.raises(TypeError, match=r"^get_markdown\(\)"):
        mod.get_markdown(SID, "o1", "extra")


def test_labels_ride_the_facade_span_and_name_public_arguments():
    # ADR-034 §3: the label on `_public` reaches span.begin; its
    # placeholders are the PUBLIC arguments (checked at export)
    _, mod, spans = _kernel()
    b = _begin(mod, spans, "update_object", SID, "o1", {"any": {"name": "x"}})
    assert b["label"] == "Updating {object} in {space}"
    b = _begin(mod, spans, "search", SID, "flaky test")
    assert b["label"] == "Searching for “{query}” in {space}"
    b = _begin(mod, spans, "list_members", SID)
    assert "label" not in b                 # unlabeled methods keep the old shape

    def method(self, space, object_id):
        """doc"""
    method.__any_public__ = ("getter", True, True, "Reading {objekt}")
    with pytest.raises(ValueError, match="names no argument"):
        mod._export("method", method)
