"""Wire-contract tests against a REAL any server (`-m integration`).

These pin what offline fakes cannot: the server's own behaviour on the
surfaces anybao builds on after ADR-027 — the catalog's derived chat,
datasets declared as parts and addressed by the collection the
declaration reports, the write gate on module and records collections,
the page body, the wiki tree — through the raw wire and through the
real any@v1 guest client. Run with a server up
(`ANYBAO_TEST_SERVER=http://127.0.0.1:7142 uv run pytest -m integration`).

Host composition (deploy → resolve → guest, the runner) is exercised by
the runtime binary in tests/test_rt_e2e.py and by cargo; it is not
re-tested here.
"""

import pytest
from conftest import AnyError

pytestmark = pytest.mark.integration


# --- the chat is the catalog's (ADR-027 §1) ----------------------------------

def test_general_chat_is_derived_idempotent_and_the_only_chat(client, fresh_space):
    first = client.catalog_setup("general-chat", fresh_space)["bundles"][0]
    again = client.catalog_setup("general-chat", fresh_space)["bundles"][0]
    assert first["installed"] is True and again["installed"] is False
    root = first["bundle"]["rootId"]
    assert again["bundle"]["rootId"] == root and first["bundle"]["derived"] is True
    assert first["typeId"] == root                       # the root is its own type
    # the registry lists it under the system id; a client cannot ensure one
    ids = {b["id"] for b in client.call("GET", f"/v1/spaces/{fresh_space}/bundles")["bundles"]}
    assert "system:general-chat/v1" in ids
    with pytest.raises(AnyError) as ei:
        client.ensure_bundle(fresh_space, {"id": "system:x/v1", "name": "x", "xKey": "xx"})
    assert ei.value.code == "bundle.reserved"
    # the chat module is reserved: no client part may declare it
    with pytest.raises(AnyError) as ei:
        client.ensure_bundle(fresh_space, {"id": "mine/v1", "name": "m", "parts": [
            {"key": "chat", "datasets": [{"module": "chat", "shared": True}]}]})
    assert ei.value.code == "dataset.module_reserved"
    # messages land on the root
    client.chat_send(fresh_space, root, {"text": "hello"})
    rows = client.query(fresh_space, root, "chat_messages", sort=["-createdAt"], limit=1)
    assert rows and rows[0]["text"] == "hello"
    # the turn log derives under the catalog bundle, deterministically
    a = client.bundle_child(fresh_space, "system:general-chat/v1", "bao/log/v1")
    assert client.bundle_child(fresh_space, "system:general-chat/v1", "bao/log/v1") == a


# --- stores are parts; the collection is read, never composed (§2) ----------

def test_dataset_declared_as_a_part_lives_in_the_reported_collection(client, fresh_space):
    tid = client.create_type(fresh_space, {"name": "Agent Log", "xKey": "agent_log",
                                           "hidden": True})["typeId"]
    client.add_part(fresh_space, tid, {"key": "agent_turns", "datasets": [
        {"key": "agent_turns", "idRule": "user", "deleteBy": "author",
         "fields": [{"key": "seq", "kind": "number"},
                    {"key": "creator", "stamp": "creator"},
                    {"key": "createdAt", "stamp": "createTime"}]}]})
    [d] = [d for d in client.list_datasets(fresh_space, tid) if d["key"] == "agent_turns"]
    assert d["collection"] == f"{tid}_agent_turns" and d["module"] == "records"
    assert "name" not in d
    # re-declaring the key is a conflict, never a second declaration
    with pytest.raises(AnyError) as ei:
        client.add_part(fresh_space, tid, {"key": "agent_turns",
                                           "datasets": [{"key": "agent_turns"}]})
    assert ei.value.code == "dataset.key_conflict"
    host = client.create_object(fresh_space, {"types": [tid]})["objectId"]
    client.upsert_record(fresh_space, host, d["collection"], "00000001", {"seq": 1})
    [row] = client.query(fresh_space, host, d["collection"])
    assert row["seq"] == 1 and "createdAt" in row and "_addSeq" in row
    # the bare key is no collection: a write fails loud, a read is silent
    with pytest.raises(AnyError) as ei:
        client.upsert_record(fresh_space, host, "agent_turns", "00000002", {"seq": 2})
    assert ei.value.code == "dataset.unknown"
    assert client.query(fresh_space, host, "agent_turns") == []
    # a host without the declaring type is refused
    other = client.create_object(fresh_space, {})["objectId"]
    with pytest.raises(AnyError) as ei:
        client.upsert_record(fresh_space, other, d["collection"], "x", {"seq": 3})
    assert ei.value.code == "dataset.not_declared"
    # the hidden type stays out of the plain listing, in the hidden one
    plain = client.call("GET", f"/v1/spaces/{fresh_space}/types")["types"]
    assert all(t["id"] != tid for t in plain)
    assert any(t["id"] == tid and t.get("hidden") for t in client.list_types(fresh_space))


def test_guest_stores_resolve_keys_turns_and_memory_land(client, bao_space, guest_use):
    c = guest_use("any@v1")
    chat = c.general_chat(bao_space)
    # the guest names stores by key; the wire carries the collection
    r = c.append_turn(bao_space, chat, {"userText": "hi", "replies": ["yo"],
                                        "llm": {"stopReason": "done"}})
    assert r == {"recordIds": ["00000001"], "seq": 1}
    log = c.chat_log(bao_space, chat)["objectId"]
    assert client.bundle_child(bao_space, "system:general-chat/v1", "bao/log/v1") == log
    coll = client.collection(bao_space, "agent_log", "agent_turns")
    assert coll and client.query(bao_space, log, coll)[0]["searchText"] == "hi yo"
    assert c.query(bao_space, log, "agent_turns")[0]["seq"] == 1
    r2 = c.append_turn(bao_space, chat, {"userText": "again", "llm": {"stopReason": "done"}})
    assert r2["seq"] == 2                                   # client-assigned, monotonic
    c.create_chunk(bao_space, chat, {"summary": "L1", "level": 1, "fromSeq": 1, "toSeq": 2,
                                     "periodStart": c.instant(1), "periodEnd": c.instant(2)})
    chunks = c.query(bao_space, log, "agent_chunks")
    assert [(x["seq"], x["level"]) for x in chunks] == [(1, 1)]
    # memory on the brain child, read back by key and by collection
    mid = c.create_memory(bao_space, {"category": "fact", "context": "teal"})["recordIds"][0]
    brain = c.get_brain(bao_space)["objectId"]
    assert client.bundle_child(bao_space, "bao/v1", "bao/brain/v1") == brain
    [item] = c.query(bao_space, brain, "agent_memory_items", filter={"id": mid})
    assert item["context"] == "teal"
    mcoll = client.collection(bao_space, "agent_brain", "agent_memory_items")
    assert client.query(bao_space, brain, mcoll, filter={"id": mid})
    # a key none of the object's types declare errors client-side
    with pytest.raises(ValueError, match="no type declaring"):
        c.query(bao_space, brain, "agent_turns")


# --- trigger datasets (plain, upsert round-trip through the guest) ----------

def test_trigger_datasets_persist_and_read_back(client, bao_space, guest_use):
    c = guest_use("any@v1")
    c.create_type(bao_space, {"name": "Agent Trigger", "xKey": "agent_trigger", "hidden": True})
    for key in ("agent_triggers", "agent_trigger_runs"):
        c._create_dataset(bao_space, "agent_trigger", {
            "key": key, "idRule": "user", "deleteBy": "anyone", "dynamic": True, "fields": []})
    tid = next(t["id"] for t in client.list_types(bao_space) if t.get("xKey") == "agent_trigger")
    anchor = client.bundle_child(bao_space, "bao/v1", "bao/triggers/v1", [tid])
    definition = {"name": "memory sweep", "kind": "cron",
                  "spec": {"cron": "0 * * * *"}, "program": "evolve@v1",
                  "args": {"space": bao_space}, "owner": "inst-A",
                  "enabled": True, "limits": {"fuelPerRun": 5_000_000}}
    c.upsert_record(bao_space, anchor, "agent_triggers", "sweep1", definition)
    [got] = c.query(bao_space, anchor, "agent_triggers", filter={"id": "sweep1"})
    assert got["kind"] == "cron" and got["spec"] == {"cron": "0 * * * *"}
    assert got["owner"] == "inst-A" and got["limits"] == {"fuelPerRun": 5_000_000}
    run = {"triggerId": "sweep1", "ts": 1_000_000, "status": "ok",
           "durationMs": 42, "fuel": 1234, "traceRef": "run_local_1"}
    c.upsert_record(bao_space, anchor, "agent_trigger_runs", "run1", run)
    [r] = c.query(bao_space, anchor, "agent_trigger_runs", filter={"id": "run1"})
    assert r["fuel"] == 1234 and r["status"] == "ok"
    # the wire carried the collections, not the keys
    assert client.query(bao_space, anchor,
                        client.collection(bao_space, "agent_trigger", "agent_triggers"))


# --- bodies on page, the wiki tree (§3) -------------------------------------

def test_body_needs_page_and_the_guest_attaches_it(client, fresh_space, guest_use):
    bare = client.create_object(fresh_space,
                                {"initialProperties": {"any": {"name": "n"}}})["objectId"]
    with pytest.raises(AnyError) as ei:
        client.put_markdown(fresh_space, bare, "# no")
    assert ei.value.code == "dataset.not_declared"
    c = guest_use("any@v1")
    c.put_markdown(fresh_space, bare, "# yes")             # attaches page first
    assert client.get_markdown(fresh_space, bare) == "# yes"
    row = client.query_objects(fresh_space, filter={"id": bare})[0]
    assert "page" in row["any"]["types"]
    oid = c.create_object(fresh_space, {"name": "Doc", "markdown": "# doc\n\nbody"})["objectId"]
    assert client.get_markdown(fresh_space, oid) == "# doc\n\nbody"


def test_parent_places_objects_in_the_wiki_tree(client, fresh_space, guest_use):
    c = guest_use("any@v1")
    top = c.create_object(fresh_space, {"name": "Top"}, parent="",
                          folder=True)["objectId"]
    kid = c.create_object(fresh_space, {"name": "Kid", "markdown": "k"}, parent=top)["objectId"]
    kid2 = c.create_object(fresh_space, {"name": "Kid 2"}, parent=top)["objectId"]
    assert [r["id"] for r in c.list_children(fresh_space, "")] == [top]
    assert [r["id"] for r in c.list_children(fresh_space, top)] == [kid, kid2]
    wiki = next(b for b in client.call("GET", f"/v1/spaces/{fresh_space}/bundles")["bundles"]
                if b["id"] == "system:wiki/v1")
    row = client.query_objects(fresh_space, filter={"id": kid})[0]
    assert wiki["rootId"] in row["any"]["types"] and "page" in row["any"]["types"]
    c.move_object(fresh_space, kid2, "")
    assert [r["id"] for r in c.list_children(fresh_space, "")] == [top, kid2]
    # the sidebar: wiki + chat are apps with descriptions from the catalog
    client.general_chat(fresh_space)
    apps = {a["bundleId"]: a for a in c.list_apps(fresh_space) if a.get("bundleId")}
    wiki_app = apps["system:wiki/v1"]
    assert wiki_app["usecase"] == "wiki" and wiki_app["description"]
    assert apps["system:general-chat/v1"]["usecase"] == "general-chat"
    installed = {a["usecase"]: a["installed"] for a in c.list_available_apps(fresh_space)}
    assert installed["wiki"] and installed["general-chat"] and not installed["crm"]
