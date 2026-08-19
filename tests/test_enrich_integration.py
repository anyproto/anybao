"""enrich@v1 against a REAL any server (`-m integration`), executed
through the REAL guest kernel (tests/kernelenv.py): any@v1 loads its
actual source and talks live http; only llm@v1 is scripted — canned
JSON citing REAL block/object ids — because cognition is pinned offline
in test_enrich_program.py. This file pins the wire contract:
editor_blocks reads, scope-basic grounding, the userspace store ensure
(enrichments hub type + runtime datasets by xKey), enrich_proposal
draft writes, and the client-side deterministic apply (grouped target
creation, real property set, hub-joined enriched_data provenance,
proposal deletion, empty-proposal idempotence). Run with a server up
(`any run --addr 127.0.0.1:7009`)."""

import json
import time

import pytest
from conftest import _http_request
from kernelenv import load_kernel

pytestmark = pytest.mark.integration


@pytest.fixture
def enrich_mod(any_server):
    """The enrich@v1 module under the real kernel: any@v1 real (live
    http against any_server), llm@v1 scripted. Push replies onto
    `.scripted` before each propose/analyze call."""
    scripted = []

    def chat(messages, system="", tier="codegen", tools=None,
             max_tokens=None):
        assert scripted, "unscripted llm call"
        return {"parts": [{"type": "text", "text": scripted.pop(0)}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    def eff(name, payload):
        if name.startswith("http."):
            return _http_request(
                name.split(".")[1].upper(), payload["url"],
                params=payload.get("params"),
                headers=payload.get("headers"),
                json_body=payload.get("json"), body=payload.get("body"),
                timeout=payload.get("timeout"))
        if name == "config.get":
            return {"value": {"any.base_url": any_server}[payload["key"]]}
        if name == "time.now":
            return {"epoch": time.time()}
        raise AssertionError(f"unexpected effect in test shim: {name}")

    app = load_kernel(effect=eff, llm_chat=chat)
    mod = app.use("enrich@v1")
    mod.scripted = scripted
    return mod


def test_propose_apply_roundtrip(client, fresh_space, enrich_mod):
    sp = fresh_space
    # target surface: a user type with a string property + one object
    tid = client.create_type(sp, {"name": "Project",
                                  "xKey": "project"})["typeId"]
    pid = client.add_property(sp, tid, {"name": "Status", "xKey": "status",
                                        "kind": "string"})["propId"]
    target = client.create_object(sp, {
        "types": [tid],
        "initialProperties": {"any": {"name": "Project Phoenix"}}},
    )["objectId"]

    # the transcript: an object whose editor body holds the notes
    tr = client.create_object(sp, {
        "types": [tid],
        "initialProperties": {"any": {"name": "Weekly sync"}}})["objectId"]
    client.call("PUT", f"/v1/spaces/{sp}/objects/{tr}/editor/markdown",
                {"content": "Phoenix moves to beta\n\n"
                            "New workstream: billing revamp\n\n"
                            "Dana owns the billing revamp\n"})
    blocks = [b for b in client.query(sp, tr, "editor_blocks")
              if (b.get("text") or "").strip()]
    assert len(blocks) >= 3
    b1, b2, b3 = (b["id"] for b in blocks[:3])

    # scripted cognition citing the REAL ids the server minted
    extract = [
        {"id": "u1", "kind": "fact", "statement": "Phoenix is in beta",
         "entities": ["Project Phoenix"], "sourceBlocks": [b1],
         "support": ["Phoenix moves to beta"]},
        {"id": "u2", "kind": "fact", "statement": "Billing revamp started",
         "entities": ["billing revamp"], "sourceBlocks": [b2],
         "support": []},
        {"id": "u3", "kind": "fact", "statement": "Dana owns billing revamp",
         "entities": ["billing revamp"], "sourceBlocks": [b3],
         "support": []},
    ]
    reconcile = {"actions": [
        {"unitId": "u1", "outcome": "enrich", "targetObjectId": target,
         "update": {"kind": "propUpdate", "field": "project.status",
                    "value": "beta"}, "reason": "phoenix exists"},
        # two facets of one topic grouped on the same newType+newName —
        # apply must mint ONE object holding both facts
        {"unitId": "u2", "outcome": "new", "targetObjectId": None,
         "newType": "project", "newName": "Billing revamp",
         "update": {"kind": None}, "reason": "no match"},
        {"unitId": "u3", "outcome": "new", "targetObjectId": None,
         "newType": "project", "newName": "Billing revamp",
         "update": {"kind": None}, "reason": "no match"},
    ]}
    enrich_mod.scripted += [json.dumps(extract), json.dumps(reconcile)]

    p = enrich_mod.propose(sp, tr)
    assert p["ok"], p
    assert p["items"] == 3 and p["errors"] == 0
    assert p["tally"] == {"enrich": 1, "new": 2}

    # the draft: one enrich_proposal_items record per item, sources cite
    # the real blocks
    items = client.query(sp, p["proposalId"], "enrich_proposal_items")
    assert len(items) == 3
    by_outcome = {}
    for it in items:
        by_outcome.setdefault(it["outcome"], []).append(it)
    assert by_outcome["enrich"][0]["source"] == \
        f"any://o/{sp}/{tr}/editor_blocks/{b1}"
    assert by_outcome["enrich"][0]["targetProperty"] == "project.status"
    assert {it["newName"] for it in by_outcome["new"]} == {"Billing revamp"}

    r = enrich_mod.apply(sp, p["proposalId"])
    assert r["ok"], r
    assert r == {"ok": True, "proposalId": p["proposalId"], "created": 1,
                 "propertiesSet": 1, "enrichedDataWritten": 3,
                 "proposalDeleted": True, "failures": []}

    # propose ensured ONE Enrichments hub; every fact rides it, joined
    # to its target by targetObjectId
    hubs = client.query_objects(sp, filter={"any.name": "Enrichments"})
    assert len(hubs) == 1
    facts = client.query(sp, hubs[0]["id"], "enriched_data")
    assert len(facts) == 3
    assert all(f.get("createdAt") and f.get("createdBy")
               for f in facts)  # schema-stamped

    # the grouped new object exists once, with BOTH facts joined to it,
    # each keeping its own source
    news = client.query_objects(sp, filter={"any.name": "Billing revamp"})
    assert len(news) == 1
    new_facts = [f for f in facts if f["targetObjectId"] == news[0]["id"]]
    assert {f["source"] for f in new_facts} == \
        {f"any://o/{sp}/{tr}/editor_blocks/{b2}",
         f"any://o/{sp}/{tr}/editor_blocks/{b3}"}

    # the property landed on the target AND its provenance is recorded
    obj = client.query_objects(sp, filter={"id": target})[0]
    assert obj[tid][pid] == "beta"
    prov = [f for f in facts if f["targetObjectId"] == target]
    assert len(prov) == 1
    assert prov[0]["target"] == "project.status"
    assert prov[0]["value"] == "beta"
    assert prov[0]["source"] == f"any://o/{sp}/{tr}/editor_blocks/{b1}"

    # idempotence: the proposal object is gone; re-apply reports empty
    r2 = enrich_mod.apply(sp, p["proposalId"])
    assert r2["ok"] is False and "empty proposal" in r2["error"]


def test_propose_grounds_against_live_search(client, fresh_space,
                                             enrich_mod):
    """The grounding leg only: scope-basic search runs against the live
    index and never surfaces the transcript itself. Index freshness is
    not asserted (indexing is async) — only the wire shape."""
    sp = fresh_space
    tid = client.create_type(sp, {"name": "Note", "xKey": "note"})["typeId"]
    tr = client.create_object(sp, {
        "types": [tid],
        "initialProperties": {"any": {"name": "call notes"}}})["objectId"]
    client.call("PUT", f"/v1/spaces/{sp}/objects/{tr}/editor/markdown",
                {"content": "zeppelin engineering review went well\n"})
    b1 = next(b["id"] for b in client.query(sp, tr, "editor_blocks")
              if (b.get("text") or "").strip())

    extract = [{"id": "u1", "kind": "fact",
                "statement": "zeppelin engineering review went well",
                "entities": ["zeppelin"], "sourceBlocks": [b1],
                "support": []}]
    enrich_mod.scripted += [json.dumps(extract),
                            json.dumps({"actions": []})]
    out = enrich_mod.analyze(sp, {"transcriptId": tr})
    assert out["ok"], out
    assert all(c["objectId"] != tr for c in out["grounded"]["u1"])


def test_apply_unknown_proposal_is_a_clean_error(fresh_space, enrich_mod):
    out = enrich_mod.apply(fresh_space, "bafynonexistent")
    assert out["ok"] is False
    assert "empty proposal" in out["error"] or "apply failed" in out["error"]
