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


def capturing():
    cap = []
    return Helper(AnyClient(fake_any(cap)), default_space="s1"), cap


def test_chat_send_with_agent_group():
    h, cap = capturing()
    h.chat_send("chat1", "hello", agent={"name": "bao", "done": True})
    m, p, b = next(x for x in cap if x[1].endswith("/chat/messages"))
    assert m == "POST" and b == {"text": "hello", "agent": {"name": "bao", "done": True}}


def test_chat_react_path():
    h, cap = capturing()
    h.chat_react("chat1", "msg9", "👍")
    m, p, _ = next(x for x in cap if "/reactions/" in x[1])
    assert m == "POST" and p.endswith("/chat/messages/msg9/reactions/👍")


def test_append_markdown_is_content():
    h, cap = capturing()
    h.append_markdown("doc1", "## more")
    m, p, b = next(x for x in cap if x[1].endswith("/editor/markdown/append"))
    assert m == "POST" and b == {"content": "## more"}


def test_create_block_shape():
    h, cap = capturing()
    h.create_block("doc1", type="paragraph", text="hi", parent_id="b0", pos="aa")
    m, p, b = next(x for x in cap if x[1].endswith("/editor/blocks"))
    assert b == {"type": "paragraph", "text": "hi", "nav": {"parentId": "b0", "pos": "aa"}}


def test_patch_block_set_unset():
    h, cap = capturing()
    h.patch_block("doc1", "b1", set={"text": "x"}, unset=["style.level"])
    m, p, b = next(x for x in cap if "/editor/blocks/b1" in x[1])
    assert m == "PATCH" and b == {"set": {"text": "x"}, "unset": ["style.level"]}


def test_ui_open_commands():
    h, cap = capturing()
    h.open_space("s2")
    h.open_object("s2", "o9", source="agent")
    cmds = [b for m, p, b in cap if p == "/v1/ui/commands"]
    assert cmds[0] == {"action": "open_space", "spaceId": "s2"}
    assert cmds[1] == {"action": "open_object", "spaceId": "s2",
                       "objectId": "o9", "source": "agent"}


def test_create_type_invalidates_catalog():
    cap = []
    h = Helper(AnyClient(fake_any_with_type_create(cap)), default_space="s1")
    h.create_object("book", {"name": "a"})   # warms catalog (1 types fetch)
    h.create_type("Movie", xkey="movie")     # should invalidate
    h.create_object("book", {"name": "b"})   # re-fetches catalog
    type_fetches = [1 for m, p, _ in cap if m == "GET" and p.endswith("/types")]
    assert len(type_fetches) == 2  # invalidation forced a refetch


def fake_any_with_type_create(cap):
    base = fake_any(cap)
    def send(method, path, body):
        if method == "POST" and path.endswith("/types"):
            return 201, {"typeId": "mv1"}
        return base(method, path, body)
    return send


def test_add_property_resolves_type_and_sets_index_meta():
    cap = []
    h = Helper(AnyClient(fake_any(cap)), default_space="s1")
    h.add_property("book", "Rating", xkey="rating", kind="number", index="basic")
    m, p, b = next(x for x in cap if x[0] == "POST" and x[1].endswith("/types/bk1/properties"))
    assert b == {"name": "Rating", "xKey": "rating",
                 "kind": "number", "meta": {"index": "basic"}}


def test_aggregate_returns_records():
    h = Helper(AnyClient(fake_any_agg()), default_space="s1")
    assert h.aggregate([{"$count": "n"}]) == [{"id": "x", "n": 3}]


def fake_any_agg():
    def send(method, path, body):
        if path.endswith("/objects/aggregate"):
            return 200, {"records": [{"id": "x", "n": 3}]}
        return 200, {}
    return send


def test_normalize_leaves_reserved_groups_unrelabeled():
    # regression (live-caught): built-in any/nav types are in the catalog
    # with display-name props (Name/Types); their record keys are already
    # canonical and must NOT be relabeled.
    types = [{"id": "any", "xKey": "", "name": "Any"},
             {"id": "bk1", "xKey": "book", "name": "Book"}]
    any_props = [{"id": "name", "xKey": "", "name": "Name"},
                 {"id": "types", "xKey": "", "name": "Types"}]

    def send(method, path, body):
        if method == "GET" and path.endswith("/types"):
            return 200, {"types": types}
        if path.endswith("/types/any/properties"):
            return 200, {"properties": any_props}
        if path.endswith("/types/bk1/properties"):
            return 200, {"properties": BOOK_PROPS}
        if path.endswith("/objects/query"):
            return 200, {"records": [{"id": "o1", "any": {"name": "Dune"},
                                      "bk1": {"p_author": "Herbert"}}]}
        return 200, {}

    h = Helper(AnyClient(send), default_space="s1")
    obj = h.get_object("o1")
    assert obj["any"] == {"name": "Dune"}         # NOT relabeled to {"Name": ...}
    assert obj["book"] == {"author": "Herbert"}   # user type IS relabeled
