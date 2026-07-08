import pytest
from anybao.anyclient import AnyClient
from anybao.helper import Helper, HelperError

# A fake `any` with one user type "book" (typeId bk1) + props author/year.
TYPES = [{"id": "bk1", "xKey": "book", "name": "Book"}]
BOOK_PROPS = [
    {"id": "p_author", "xKey": "author", "name": "Author", "kind": "string"},
    {"id": "p_year", "xKey": "year", "name": "Year", "kind": "number"},
]


def fake_any(capture=None):
    """Routes the handful of calls the helper makes."""
    def send(method, path, body):
        if capture is not None:
            capture.append((method, path, body))
        if method == "GET" and path.endswith("/types"):
            return 200, {"types": TYPES}
        if method == "GET" and path.endswith("/types/bk1/properties"):
            return 200, {"properties": BOOK_PROPS}
        if method == "POST" and path.endswith("/objects"):
            return 201, {"objectId": "obj1"}
        if method == "PUT" and "editor/markdown" in path:
            return 200, {"inserted": []}
        if method == "POST" and "/properties/" in path:
            return 200, {}
        if method == "POST" and path.endswith("/objects/query"):
            return 200, {"records": [
                {"id": "obj1", "any": {"name": "Dune"},
                 "bk1": {"p_author": "Herbert", "p_year": 1965}}]}
        return 200, {}
    return send


def helper(capture=None):
    return Helper(AnyClient(fake_any(capture)), default_space="s1")


def test_create_object_nested_shape_resolves_propids():
    cap = []
    h = helper(cap)
    res = h.create_object("book", {"name": "Dune", "book": {"author": "Herbert", "year": 1965}})
    assert res["id"] == "obj1"
    create = next(b for m, p, b in cap if m == "POST" and p.endswith("/objects"))
    assert create["types"] == ["bk1"]
    # xKeys resolved to propIds, under the type namespace + any.name
    assert create["initialProperties"] == {
        "any": {"name": "Dune"},
        "bk1": {"p_author": "Herbert", "p_year": 1965},
    }


def test_unknown_top_level_key_raises_no_silent_drop():
    h = helper()
    with pytest.raises(HelperError, match="unknown data key 'auther'"):
        h.create_object("book", {"name": "x", "auther": {}})  # typo'd type key


def test_unknown_property_raises():
    h = helper()
    with pytest.raises(HelperError, match="property 'pages' not found"):
        h.create_object("book", {"book": {"pages": 300}})


def test_dotted_key_rejected_as_write():
    h = helper()
    with pytest.raises(HelperError, match="dotted key"):
        h.create_object("book", {"book.author": "x"})


def test_unknown_type_raises():
    h = helper()
    with pytest.raises(HelperError, match="type not found: 'movie'"):
        h.create_object("movie", {"name": "x"})


def test_get_object_normalizes_to_readable_shape():
    h = helper()
    obj = h.get_object("obj1")
    assert obj["any"]["name"] == "Dune"
    assert obj["book"] == {"author": "Herbert", "year": 1965}  # propIds → xKeys, typeId → key


def test_body_markdown_set_after_create():
    cap = []
    h = helper(cap)
    h.create_object("book", {"name": "x", "body": "# notes"})
    put = next(b for m, p, b in cap if m == "PUT" and "editor/markdown" in p)
    assert put == {"content": "# notes"}  # content, NOT markdown (wire landmine)


def test_catalog_cached_within_ttl():
    cap = []
    h = helper(cap)
    h.create_object("book", {"name": "a"})
    h.create_object("book", {"name": "b"})
    type_fetches = [1 for m, p, _ in cap if m == "GET" and p.endswith("/types")]
    assert len(type_fetches) == 1  # second create reused the catalog
