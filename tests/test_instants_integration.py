"""ADR-019 instants against a real any server (`-m integration`):
date properties, agent stores, temporal recall and the bare-literal
trap — all through the real any@v1 guest client."""

import time

import pytest
from conftest import AnyError

pytestmark = pytest.mark.integration


def _instant(v):
    return isinstance(v, dict) and set(v) == {"$date"}


def test_date_property_round_trips_and_range_filters(client, fresh_space, guest_use):
    c = guest_use("any@v1")
    inst = guest_use("any@v1").instant
    c.create_type(fresh_space, {"name": "Event", "properties": [
        {"name": "When", "xFormat": {"type": "datetime"}},
        {"name": "Day", "xFormat": {"type": "date"}}]})
    kinds = {p["xKey"]: p.get("kind") for p in c.list_properties(fresh_space, "event")}
    assert kinds["when"] == "datetime" and kinds["day"] == "datetime"   # slug-derived
    t0 = 1_787_673_600            # 2026-08-25T16:00Z
    ids = {}
    for name, secs in (("early", t0 - 3600), ("late", t0 + 3600)):
        ids[name] = c.create_object(fresh_space, {
            "types": ["event"], "name": name,
            "initialProperties": {"event": {"when": inst(secs),
                                            "day": inst("2026-08-25")}}})["objectId"]
    rows = {r["id"]: r for r in c.query_objects(fresh_space, filter={"any.types": "event"})}
    when = rows[ids["early"]]["event"]["when"]
    assert _instant(when) and when["$date"].startswith("2026-08-25T15:00:00")
    assert rows[ids["early"]]["event"]["day"]["$date"].startswith("2026-08-25T00:00:00")
    assert _instant(rows[ids["early"]]["createdAt"])
    # a wrapped range literal is a real subset
    hit = c.query_objects(fresh_space, filter={"any.types": "event",
                                              "event.when": {"$lt": inst(t0)}})
    assert [r["id"] for r in hit] == [ids["early"]]
    # a bare number never reaches the wire
    with pytest.raises(ValueError, match="instant"):
        c.query_objects(fresh_space, filter={"event.when": {"$lt": t0}})
    # …because server-side it silently matches NOTHING (comparisons are
    # bracketed by type) — no error either way, hence the client guard
    pid = next(p["id"] for p in c.list_properties(fresh_space, "event") if p["xKey"] == "when")
    tid = next(t["id"] for t in c.list_types(fresh_space) if t.get("xKey") == "event")
    raw = client.call("POST", f"/v1/spaces/{fresh_space}/objects/query",
                      {"filter": {f"{tid}.{pid}": {"$gte": t0}}})
    assert raw["records"] == []


def test_agent_stores_hold_instants_and_by_period_range_scans(client, bao_space, guest_use):
    fresh_space = bao_space
    c = guest_use("any@v1")
    inst, ts_s = c.instant, c.ts_s
    chat = c.general_chat(fresh_space)
    now = int(time.time())
    # memory: validFrom defaults to instant(now()); a number is refused
    mid = c.create_memory(fresh_space,
                          {"category": "lesson", "context": "instants"})["recordIds"][0]
    brain = c.get_brain(fresh_space)["objectId"]
    item = c.query(fresh_space, brain, "agent_memory_items", filter={"id": mid})[0]
    assert _instant(item["validFrom"]) and abs(ts_s(item["validFrom"]) - now) < 120
    assert _instant(item["createdAt"])
    with pytest.raises(AnyError, match="kind mismatch"):
        client.call("POST", f"/v1/spaces/{fresh_space}/modify", {
            "objectId": brain,
            "dataset": client.collection(fresh_space, "agent_brain", "agent_memory_items"),
            "records": [{"id": "", "upsert": True, "ops": [
                {"type": "$set", "path": "category", "value": "x"},
                {"type": "$set", "path": "context", "value": "x"},
                {"type": "$set", "path": "validFrom", "value": now}]}]})
    # turns + chunks on the log child; periods are the turns' own instants
    c.append_turn(fresh_space, chat, {"userText": "hello", "replies": ["hi"],
                                      "llm": {"stopReason": "done"}})
    log = c.chat_log(fresh_space, chat)["objectId"]
    turn = c.query(fresh_space, log, "agent_turns")[0]
    assert _instant(turn["createdAt"])
    c.create_chunk(fresh_space, chat, {"summary": "hour one", "level": 1,
                                       "fromSeq": 1, "toSeq": 1,
                                       "periodStart": turn["createdAt"],
                                       "periodEnd": inst(now)})
    chunk = c.query(fresh_space, log, "agent_chunks")[0]
    assert chunk["periodStart"] == turn["createdAt"] and _instant(chunk["periodEnd"])
    # by_period: one instant range per source, merged
    r = guest_use("recall@v1").recall(c, fresh_space, chat_object_id=chat)
    got = {(x["source"], x["id"]) for x in r.by_period(now - 300, now + 300)}
    assert {("memory", mid), ("turn", turn["id"]), ("chunk", chunk["id"])} <= got
    assert r.by_period(now + 3600, now + 7200) == []
    # native date arithmetic over the log
    act = guest_use("history@v1").activity(c, fresh_space, chat, unit="day")
    assert len(act) == 1 and act[0]["turns"] == 1 and _instant(act[0]["period"])
