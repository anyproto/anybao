"""programs/enrich@v1 — the transcript-enrichment tool, run through the
REAL guest kernel (tests/kernelenv.py: runtime/guest/app.py imported
host-side, wit_world stubbed) so kernel semantics are enforced —
curated builtins, the import allowlist, span facades, use() loading.
Cognition is scripted through fake any@v1 / llm@v1 shims at the effect
boundary; what is pinned here is the mechanical contract: block
citation + hallucination filtering, grounding exclusions, action→item
mapping (conflict demoted, redundant dropped, empty fields skipped),
provenance urls, the userspace-store ensure, and the deterministic
client-side apply (grouped mints, property sets, hub-joined facts)."""

import json

from kernelenv import load_kernel


class FakeGuest:
    """One object answering the any@v1 client + llm@v1 chat + effect
    surface the enrich program uses."""

    def __init__(self, *, blocks=(), hits=(), types=(), objects=(),
                 hubs=(), proposal_items=(), llm_texts=()):
        self.blocks = list(blocks)
        self.hits = list(hits)
        self.types = list(types)
        self.objects = list(objects)
        self.hubs = list(hubs)
        self.proposal_items = list(proposal_items)
        self.llm_texts = list(llm_texts)
        self.llm_calls = []
        self.searches = []
        self.created = []
        self.created_types = []
        self.datasets = []
        self.updates = []
        self.deleted = []
        self.modifies = []
        self.markdowns = []

    # --- any@v1 client ---
    def query(self, space, object_id, dataset, **opts):
        if dataset == "editor_blocks":
            return list(self.blocks)
        if dataset == "enrich_proposal_items":
            return list(self.proposal_items)
        raise AssertionError(f"unexpected dataset {dataset}")

    def search(self, space, query, scopes=None, limit=None, mode=None):
        self.searches.append({"query": query, "scopes": scopes,
                              "limit": limit})
        return {"hits": list(self.hits)}

    def list_types(self, space):
        return list(self.types)

    def create_type(self, space, body):
        self.created_types.append(body)
        return {"typeId": f"t-{body['xKey']}", "xKey": body["xKey"],
                "created": True, "addedProps": {}}

    def create_dataset(self, space, type_key, draft):
        self.datasets.append({"type": type_key, "name": draft["name"]})
        return {"datasetDefId": f"d-{draft['name']}", "created": True}

    def query_objects(self, space, **opts):
        flt = opts.get("filter") or {}
        if "any.types" in flt:
            return list(self.hubs)
        if "id" in flt:
            return [o for o in self.objects if o.get("id") == flt["id"]]
        return list(self.objects)

    def create_object(self, space, body):
        self.created.append(body)
        return {"objectId": f"obj{len(self.created)}"}

    def update_object(self, space, object_id, body):
        self.updates.append({"objectId": object_id, "body": body})
        return {"objectId": object_id}

    def delete_object(self, space, object_id):
        self.deleted.append(object_id)
        return {}

    def modify(self, space, body):
        self.modifies.append(body)
        return {"recordIds": ["r"]}

    def put_markdown(self, space, object_id, content):
        self.markdowns.append({"objectId": object_id, "content": content})
        return {}

    # --- llm@v1 ---
    def chat(self, messages, system="", tier="codegen", tools=None,
             max_tokens=None):
        self.llm_calls.append({"system": system, "tier": tier,
                               "max_tokens": max_tokens,
                               "prompt": messages[0]["parts"][0]["text"]})
        return {"parts": [{"type": "text", "text": self.llm_texts.pop(0)}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    # --- pass-through effects (nothing left but config lookups) ---
    def effect(self, name, payload):
        if name == "config.get":
            return {"value": "http://any.test"}
        raise AssertionError(f"unexpected effect {name}")


def load(fake):
    app = load_kernel(effect=fake.effect, any_client=fake,
                      llm_chat=fake.chat)
    return app.use("enrich@v1")


EXTRACT = [
    {"id": "u1", "kind": "decision", "statement": "SQLite was chosen",
     "entities": ["storage"], "sourceBlocks": ["b1", "bogus"],
     "support": ["we go sqlite"]},
    {"id": "u2", "kind": "task", "statement": "Alice owns the migration",
     "entities": ["migration"], "sourceBlocks": ["b2"], "support": []},
    {"id": "u3", "kind": "fact", "statement": "Deadline moved to June",
     "entities": ["deadline"], "sourceBlocks": ["b1", "b2"],
     "support": []},
    {"id": "u4", "kind": "fact", "statement": "The project exists",
     "entities": [], "sourceBlocks": ["b1"], "support": []},
]

RECONCILE = {"actions": [
    {"unitId": "u1", "outcome": "enrich", "targetObjectId": "obj9",
     "newType": None, "newName": None,
     "update": {"kind": "propUpdate", "field": "project.status",
                "value": "sqlite chosen"},
     "reason": "matches Project X"},
    {"unitId": "u2", "outcome": "new", "targetObjectId": None,
     "newType": "pages", "newName": "Migration plan",
     "update": {"kind": None, "field": None, "value": ""},
     "reason": "no match"},
    {"unitId": "u3", "outcome": "conflict", "targetObjectId": "obj9",
     "newType": None, "newName": None,
     "update": {"kind": "bodyUpdate", "field": None,
                "value": "deadline June"},
     "reason": "contradicts May"},
    {"unitId": "u4", "outcome": "redundant", "targetObjectId": "obj9",
     "newType": None, "newName": None,
     "update": {"kind": None, "field": None, "value": ""},
     "reason": "already known"},
]}


def happy_fake():
    return FakeGuest(
        blocks=[{"id": "b1", "text": "we go sqlite"},
                {"id": "b2", "text": "alice takes migration"},
                {"id": "b3", "text": "   "}],          # blank → not cited
        hits=[{"objectId": "tr1", "data": "self hit"},  # transcript: skip
              {"objectId": "obj9", "title": "Project X",
               "data": "a project snippet"}],
        types=[{"id": "t-user", "xKey": "pages", "name": "Page",
                "builtIn": False, "description": "a page"},
               {"id": "editor", "xKey": "editor", "name": "Editor",
                "builtIn": True},
               {"id": "chat", "xKey": "chat", "name": "Chat",
                "builtIn": True}],                      # filtered out
        objects=[{"id": "tr1", "any": {"name": "Weekly sync"}}],
        hubs=[{"id": "hub1"}],  # the store exists; ensure finds it
        llm_texts=[json.dumps(EXTRACT), json.dumps(RECONCILE)])


def item_of(modify_call):
    return {op["path"]: op["value"]
            for op in modify_call["records"][0]["ops"]}


def test_kernel_import_allowlist_enforced():
    """The point of kernel-fidelity: imports outside the tier-1
    allowlist must fail at load, as in the wasm guest — while allowed
    pure-stdlib modules (contextlib since ADR-002 §4, 2026-07-22)
    pass."""
    fake = happy_fake()
    app = load_kernel(effect=fake.effect, any_client=fake,
                      llm_chat=fake.chat)
    exec(compile("import contextlib", "<probe>", "exec"),
         dict(app._fresh_ns()))
    try:
        exec(compile("import sys", "<probe>", "exec"),
             dict(app._fresh_ns()))
    except ImportError as e:
        assert "outside the effect boundary" in str(e)
    else:
        raise AssertionError("sys import should have been blocked")


def test_propose_maps_actions_to_sourced_items():
    fake = happy_fake()
    out = load(fake).propose("s1", "tr1")

    assert out["ok"] and out["proposalId"] == "obj1"
    assert out["items"] == 3 and out["errors"] == 0
    assert out["tally"] == {"enrich": 2, "new": 1, "redundant": 1}
    assert out["proposalLink"] == "any://o/s1/obj1"

    items = [item_of(m) for m in fake.modifies]
    assert [m["dataset"] for m in fake.modifies] == \
        ["enrich_proposal_items"] * 3

    # u1: property enrichment; hallucinated "bogus" block dropped;
    # source = the canonical dataset-record URI (WEB-42)
    assert items[0]["outcome"] == "enrich"
    assert items[0]["source"] == "any://o/s1/tr1/editor_blocks/b1"
    assert items[0]["targetKind"] == "property"
    assert items[0]["targetProperty"] == "project.status"
    assert items[0]["value"] == "sqlite chosen"
    assert items[0]["targetObjectId"] == "obj9"
    # u2: new object; collection kind; no property fields written
    assert items[1]["outcome"] == "new"
    assert items[1]["newType"] == "pages"
    assert items[1]["newName"] == "Migration plan"
    assert items[1]["targetKind"] == "collection"
    assert "targetProperty" not in items[1] and "value" not in items[1]
    # u3: conflict demoted to enrich for review; multi-block source =
    # one record URI per cited block, comma-joined
    assert items[2]["outcome"] == "enrich"
    assert items[2]["source"] == ("any://o/s1/tr1/editor_blocks/b1,"
                                  "any://o/s1/tr1/editor_blocks/b2")
    assert items[2]["text"] == "Deadline moved to June"

    # the userspace store was ensured: both types + both datasets, and
    # the existing hub was found (never re-created)
    assert [t["xKey"] for t in fake.created_types] == \
        ["enrichments", "enrich_proposal"]
    assert fake.datasets == [
        {"type": "enrichments", "name": "enriched_data"},
        {"type": "enrich_proposal", "name": "enrich_proposal_items"}]

    # proposal object: typed + named after the transcript, body written
    assert fake.created[0]["types"] == ["enrich_proposal"]
    name = fake.created[0]["initialProperties"]["any"]["name"]
    assert name == "Enrichment proposal — Weekly sync"
    assert fake.markdowns[0]["objectId"] == "obj1"
    assert "3 proposed enrichment items" in fake.markdowns[0]["content"]


def test_grounding_excludes_transcript_and_feeds_reconcile():
    fake = happy_fake()
    out = load(fake).analyze("s1", {"transcriptId": "tr1"})

    assert out["ok"] and out["tally"] == \
        {"enrich": 1, "new": 1, "conflict": 1, "redundant": 1}
    # one scope-basic search per unit, on the clean statement
    assert [s["query"] for s in fake.searches] == \
        [u["statement"] for u in EXTRACT]
    assert all(s["scopes"] == ["basic"] for s in fake.searches)
    # the transcript never grounds itself; titles ride along
    for cands in out["grounded"].values():
        assert all(c["objectId"] != "tr1" for c in cands)
    assert out["grounded"]["u1"][0]["name"] == "Project X"

    # extract prompt carries cited lines, blank block omitted; both
    # passes lift llm@v1's 4096 cap (a long unit list truncates to
    # stop="length" otherwise — the daily-standup live failure)
    extract_prompt = fake.llm_calls[0]["prompt"]
    assert "[b1] we go sqlite" in extract_prompt
    assert "b3" not in extract_prompt
    assert [c["max_tokens"] for c in fake.llm_calls] == [32000, 24000]
    # reconcile prompt sees only user types + editor
    reconcile_prompt = fake.llm_calls[1]["prompt"]
    assert "pages — Page" in reconcile_prompt
    assert "chat — Chat" not in reconcile_prompt


def test_extract_retries_once_then_reports():
    fake = happy_fake()
    fake.llm_texts = ["no json here at all", "still prose"]
    out = load(fake).analyze("s1", {"transcriptId": "tr1"})
    assert out == {"ok": False, "error": "no knowledge units extracted",
                   "transcriptChars": out["transcriptChars"],
                   "rawExtractHead": "still prose"}
    assert len(fake.llm_calls) == 2  # one retry, then give up


def test_analyze_raw_transcript_skips_block_validation():
    fake = happy_fake()
    fake.llm_texts = [json.dumps(EXTRACT), json.dumps(RECONCILE)]
    out = load(fake).analyze("s1", {"transcript": "plain notes"})
    assert out["ok"] and out["transcriptId"] is None
    # raw mode: nothing to validate citations against — kept verbatim
    assert out["units"][0]["sourceBlocks"] == ["b1", "bogus"]


def test_parse_tolerates_fences_prose_and_bad_escapes():
    parse = load(happy_fake())._parse
    assert parse('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert parse('Here you go: {"a": 1} hope that helps') == {"a": 1}
    assert parse('["snake\\_case"]') == ["snake_case"]  # illegal \_ repaired
    assert parse("no json") is None and parse("") is None


def test_parse_salvages_a_length_truncated_array():
    """A stop="length" reply cuts the unit array mid-element (the live
    daily-standup failure); the parser drops the incomplete tail
    instead of failing closed. Truncated non-arrays still return None."""
    parse = load(happy_fake())._parse
    cut = '```json\n[{"id":"u1","statement":"done"},{"id":"u2","state'
    assert parse(cut) == [{"id": "u1", "statement": "done"}]
    assert parse('{"actions": [{"unitId": "u1"') is None


def test_missing_inputs_error_without_side_effects():
    fake = happy_fake()
    mod = load(fake)
    assert mod.propose("", "tr1")["ok"] is False
    assert mod.propose("s1", "")["ok"] is False
    assert mod.apply("s1", "")["ok"] is False
    fake.blocks = []
    out = mod.propose("s1", "tr1")
    assert out["ok"] is False and "no editor blocks" in out["error"]
    assert not fake.created and not fake.modifies


def test_apply_is_deterministic_and_hub_joined():
    fake = happy_fake()
    fake.proposal_items = [
        {"id": "i1", "text": "SQLite was chosen",
         "source": "any://o/s1/tr1/editor_blocks/b1", "outcome": "enrich",
         "targetObjectId": "obj9", "targetKind": "property",
         "targetProperty": "project.status", "value": "sqlite chosen"},
        # two facets sharing newType+newName → apply mints ONE object
        {"id": "i2", "text": "Billing revamp exists",
         "source": "any://o/s1/tr1/editor_blocks/b2", "outcome": "new",
         "targetKind": "collection", "newType": "pages",
         "newName": "Billing revamp"},
        {"id": "i3", "text": "Dana owns billing revamp",
         "source": "any://o/s1/tr1/editor_blocks/b3", "outcome": "new",
         "targetKind": "collection", "newType": "pages",
         "newName": "Billing revamp"},
        {"id": "i4", "text": "orphan", "outcome": "new"},  # no target/name
    ]
    out = load(fake).apply("s1", "prop9")

    assert out["ok"] and out["proposalId"] == "prop9"
    assert out["created"] == 1 and out["propertiesSet"] == 1
    assert out["enrichedDataWritten"] == 3
    assert out["proposalDeleted"] is True
    assert out["failures"] == ["item i4: no target and no newType/newName"]

    # the real property landed on the existing target, xKey-addressed
    assert fake.updates == [{"objectId": "obj9",
                             "body": {"project": {"status": "sqlite chosen"}}}]
    # ONE minted object for the grouped facets
    minted = [b for b in fake.created if b.get("types") == ["pages"]]
    assert len(minted) == 1 and minted[0]["name"] == "Billing revamp"
    # every fact rides the hub, joined to its target by targetObjectId
    assert all(m["objectId"] == "hub1" and m["dataset"] == "enriched_data"
               for m in fake.modifies)
    facts = [item_of(m) for m in fake.modifies]
    assert facts[0]["targetObjectId"] == "obj9"
    assert facts[0]["target"] == "project.status"
    assert facts[0]["value"] == "sqlite chosen"
    assert facts[0]["source"] == "any://o/s1/tr1/editor_blocks/b1"
    assert facts[1]["targetObjectId"] == facts[2]["targetObjectId"] == "obj1"
    assert "target" not in facts[1] and "value" not in facts[1]
    # the proposal object is deleted last
    assert fake.deleted == ["prop9"]


def test_apply_empty_or_unknown_proposal_is_clean():
    fake = happy_fake()
    out = load(fake).apply("s1", "gone")
    assert out["ok"] is False
    assert "empty proposal" in out["error"]
    # nothing was minted, set, written, or deleted
    assert not fake.created and not fake.updates
    assert not fake.modifies and not fake.deleted
