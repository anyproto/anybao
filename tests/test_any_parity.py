"""The any API parity helpers of programs/any@v1 (docs/api-parity.md
§C10): the wire shape of each call, against the fake wire."""

from test_any_module import _CAT, client, load, wire

OBJ = "/v1/spaces/s1/objects/o1"


def _one(fx):
    """The single call a helper made → (VERB, url-with-query, body)."""
    (verb, _, body), = fx.calls
    return verb, fx.urls[0], body


def test_the_parity_helpers_are_on_the_flat_surface_with_docs():
    g = load(wire())
    for name in NAMES:
        assert callable(g[name]) and g[name].__doc__, name


# --- object history ------------------------------------------------------------

def test_list_changes_pages_and_filters():
    fx = wire(replies={"/history": {"changes": [{"version": "v2"}], "cursor": ""}})
    r = client(fx).list_changes("s1", "o1", limit=5, author="A1",
                                dataset="editor_blocks", grouped=True)
    assert r == {"changes": [{"version": "v2"}], "cursor": None}
    assert _one(fx) == ("GET", OBJ + "/history?limit=5&author=A1"
                        "&dataset=editor_blocks&coalesce=true", None)


def test_get_version_reads_the_object_row_like_a_query_row():
    fx = wire(replies={**_CAT, "/history/v1": {"version": "v1", "datasets": [
        {"dataset": "objects", "records": [
            {"id": "o1", "_ver": {"id": "!"}, "any": {"name": "Ship", "type": "bafyTASK"},
             "bafyTASK": {"bafySTATUS": "open"}}]},
        {"dataset": "editor_blocks", "records": [
            {"id": "b1", "_ver": {"id": "!"}, "_addSeq": 3, "text": "hi"}]}]}})
    r = client(fx).get_version("s1", "o1", "v1")
    objects, blocks = r["datasets"]
    assert objects["records"] == [{"id": "o1", "any": {"name": "Ship", "type": "task"},
                                   "task": {"status": "open"}}]
    assert blocks["records"] == [{"id": "b1", "text": "hi"}]
    assert fx.urls[0] == OBJ + "/history/v1"


def test_get_record_version_takes_the_fast_path():
    fx = wire(replies={"/records/b%3A1": {"exists": True, "record": {
        "id": "b:1", "_ver": {}, "text": "hi"}}})
    r = client(fx).get_record_version("s1", "o1", "v1", "editor_blocks", "b:1")
    assert r == {"exists": True, "record": {"id": "b:1", "text": "hi"}}
    assert fx.urls[0] == OBJ + "/history/v1/datasets/editor_blocks/records/b%3A1"


def test_diff_versions_names_the_property_paths():
    fx = wire(replies={**_CAT, "/history/diff": {"base": "", "version": "v2", "datasets": [
        {"dataset": "objects", "records": [{"id": "o1", "kind": "changed", "fields": [
            {"path": ["any", "name"], "before": "A", "after": "B"},
            {"path": ["bafyTASK", "bafySTATUS"], "after": "done"}]}]},
        {"dataset": "editor_blocks", "records": [{"id": "b1", "kind": "changed",
                                                  "fields": [{"path": ["text"]}]}]}]}})
    r = client(fx).diff_versions("s1", "o1", "v2", base="v1")
    paths = [f["path"] for f in r["datasets"][0]["records"][0]["fields"]]
    assert paths == [["any", "name"], ["task", "status"]]
    assert r["datasets"][1]["records"][0]["fields"] == [{"path": ["text"]}]
    assert fx.urls[0] == OBJ + "/history/diff?version=v2&base=v1"


NAMES = ("list_changes", "get_version", "get_record_version", "diff_versions")
