"""The any API parity helpers of programs/any@v1 (docs/api-parity.md
§C10): the wire shape of each call, against the fake wire."""

import json

from test_any_module import _CAT, REF, client, load, wire

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


# --- single reads --------------------------------------------------------------

def test_get_object_reads_one_row_like_a_query_row():
    fx = wire(replies={**_CAT, "/objects/o1": {"objectId": "o1", "record": {
        "id": "o1", "_ver": {"id": "!"}, "any": {"name": "Ship", "type": "bafyTASK"},
        "bafyTASK": {"bafySTATUS": "open"}}}})
    c = client(fx)
    r = c.get_object("s1", "o1")
    assert r == {"id": "o1", "any": {"name": "Ship", "type": "task"},
                 "task": {"status": "open"}}
    assert ("GET", OBJ, None) in fx.calls
    assert c.get_object("s1", "o1", normalize=False)["_ver"] == {"id": "!"}


def test_get_object_is_none_for_a_missing_object():
    fx = wire(status=404, replies={"": {"error": {"code": "object.not_found"}}})
    assert client(fx).get_object("s1", "gone") is None


def test_get_type_and_get_collection_read_one_definition():
    cat = {**_CAT, "/collections": {"collections": [
        {"id": "bafyTAG", "name": "Reading list", "xKey": "reading_list"}]},
        "/types/bafyTASK": {"id": "bafyTASK", "xKey": "task", "name": "Task"},
        "/collections/bafyTAG": {"id": "bafyTAG", "xKey": "reading_list"}}
    fx = wire(replies=cat)
    c = client(fx)
    assert c.get_type("s1", "task")["name"] == "Task"
    assert c.get_collection("s1", "reading_list")["xKey"] == "reading_list"
    gets = [p for v, p, _ in fx.calls]
    assert "/v1/spaces/s1/types/bafyTASK" in gets
    assert "/v1/spaces/s1/collections/bafyTAG" in gets


def test_get_member_reads_me_or_one_identity():
    fx = wire(replies={"/members/me": {"identity": "A1", "permission": "owner",
                                       "status": "active"},
                       "/members/A2": {"identity": "A2", "name": "Bo",
                                       "permission": "reader", "status": "active"}})
    c = client(fx)
    assert c.get_member("s1")["permission"] == "owner"
    assert c.get_member("s1", "A2") == {"identity": "A2", "name": "Bo",
                                        "permission": "reader", "status": "active"}
    assert [p for _, p, _ in fx.calls] == ["/v1/spaces/s1/members/me",
                                           "/v1/spaces/s1/members/A2"]


# --- account, device, private space settings ------------------------------------

def test_account_limits_and_local_discovery_are_plain_reads_and_one_write():
    fx = wire(replies={"/account/limits": {"storage": {"limitBytes": 0}},
                       "/local-discovery": {"enabled": False}})
    c = client(fx)
    assert c.account_limits() == {"storage": {"limitBytes": 0}}
    assert c.local_discovery() == {"enabled": False}
    c.set_local_discovery(0)
    assert fx.calls == [("GET", "/v1/account/limits", None),
                        ("GET", "/v1/local-discovery", None),
                        ("PUT", "/v1/local-discovery", {"enabled": False})]


def test_icons_come_back_as_blobs_and_none_when_absent():
    def fx(name, payload):
        if payload["url"].endswith("/account/icon"):
            return {"status": 200, "headers": {"content-type": "image/png"},
                    "body": dict(REF)}
        return {"status": 404, "headers": {}, "body": json.dumps(
            {"error": {"code": "identity.icon_not_found"}})}
    g = load(fx)
    c = g["_Client"]("http://any")
    r = c.account_icon()
    assert r["mime"] == "image/png" and isinstance(r["blob"], g["Blob"])
    assert c.identity_icon("A1") is None


def test_set_account_icon_sends_the_image_raw_and_none_removes_it():
    seen = []

    def fx(name, payload):
        seen.append((name, payload))
        return {"status": 200, "headers": {}, "body": json.dumps({"iconCid": "bafyI"})}
    g = load(fx)
    c = g["_Client"]("http://any")
    b = g["Blob"].from_ref(REF)
    assert c.set_account_icon(b, name="me 1.png") == {"iconCid": "bafyI"}
    name, payload = seen[-1]
    assert name == "http.put" and payload["body"] is b
    assert payload["url"] == "http://any/v1/account/icon?name=me%201.png"
    c.set_account_icon(None)
    assert seen[-1][0] == "http.delete" and "body" not in seen[-1][1]


def test_list_unread_pages_by_cursor():
    fx = wire(replies={"/read-state/unread": {"changes": [], "cursor": "c9"}})
    assert client(fx).list_unread("s1", cursor="c1", limit=5) == {"changes": [], "cursor": "c9"}
    assert fx.urls == ["/v1/spaces/s1/read-state/unread?cursor=c1&limit=5"]


def test_set_space_settings_patches_per_key():
    fx = wire()
    c = client(fx)
    c.set_space_settings("s1", set={"notifyMode": "mentions"}, unset=["old"])
    assert _one(fx) == ("PATCH", "/v1/spaces/s1/settings",
                        {"set": {"notifyMode": "mentions"}, "unset": ["old"]})
    try:
        c.set_space_settings("s1")
    except ValueError as e:
        assert "needs" in str(e)
    else:
        raise AssertionError("an empty patch must raise")


# --- chat messages ---------------------------------------------------------------

MSG = OBJ + "/chat/messages/m1"


def test_chat_edit_delete_and_react_hit_the_message_routes():
    fx = wire(replies={"": {"changeId": "c1", "recordIds": ["m1"]}})
    c = client(fx)
    assert c.chat_edit("s1", "o1", "m1", "new text")["recordIds"] == ["m1"]
    c.chat_react("s1", "o1", "m1", "👍")
    c.chat_delete("s1", "o1", "m1")
    assert fx.calls == [("PATCH", MSG, {"text": "new text"}),
                        ("POST", MSG + "/reactions/%F0%9F%91%8D", None),
                        ("DELETE", MSG, None)]


def test_chat_mark_read_picks_the_route_by_its_arguments():
    fx = wire()
    c = client(fx)
    c.chat_mark_read("s1", "o1")
    c.chat_mark_read("s1", "o1", "m1")
    c.chat_mark_read("s1", "o1", "m1", reactions=True)
    assert [p for _, p, _ in fx.calls] == [OBJ + "/chat/read-all", MSG + "/read",
                                           MSG + "/reactions-read"]
    try:
        c.chat_mark_read("s1", "o1", reactions=True)
    except ValueError as e:
        assert "message_id" in str(e)
    else:
        raise AssertionError("reactions without a message must raise")


NAMES = ("list_changes", "get_version", "get_record_version", "diff_versions",
         "get_object", "get_type", "get_collection", "get_member",
         "account_limits", "account_icon", "identity_icon", "set_account_icon",
         "local_discovery", "set_local_discovery", "list_unread", "set_space_settings",
         "chat_edit", "chat_delete", "chat_react", "chat_mark_read")
