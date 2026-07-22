"""programs/enrich@v1 — the transcript-enrichment tool, run through the
REAL guest kernel (tests/kernelenv.py: runtime/guest/app.py imported
host-side, wit_world stubbed) so kernel semantics are enforced —
curated builtins, the import allowlist, span facades, use() loading.
Cognition is scripted through fake any@v1 / llm@v1 shims at the effect
boundary; what is pinned here is the mechanical contract: block
citation + hallucination filtering, grounding exclusions, action→item
mapping (conflict demoted, redundant dropped, empty fields skipped),
provenance urls, and the apply wrapper's envelope."""

import json

from kernelenv import load_kernel


class FakeGuest:
    """One object answering the any@v1 client + llm@v1 chat + effect
    surface the enrich program uses."""

    def __init__(self, *, blocks=(), hits=(), types=(), objects=(),
                 llm_texts=(), apply_reply=None):
        self.blocks = list(blocks)
        self.hits = list(hits)
        self.types = list(types)
        self.objects = list(objects)
        self.llm_texts = list(llm_texts)
        self.apply_reply = apply_reply or {"status": 200, "body": "{}"}
        self.llm_calls = []
        self.searches = []
        self.created = []
        self.modifies = []
        self.markdowns = []
        self.posts = []

    # --- any@v1 client ---
    def query(self, space, object_id, dataset, **opts):
        assert dataset == "editor_blocks"
        return list(self.blocks)

    def search(self, space, query, scopes=None, limit=None, mode=None):
        self.searches.append({"query": query, "scopes": scopes,
                              "limit": limit})
        return {"hits": list(self.hits)}

    def list_types(self, space):
        return list(self.types)

    def query_objects(self, space, **opts):
        return list(self.objects)

    def create_object(self, space, body):
        self.created.append(body)
        return {"objectId": "prop1"}

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

    # --- pass-through effects (config + the direct /enrich/apply POST) ---
    def effect(self, name, payload):
        if name == "config.get":
            return {"value": "http://any.test"}
        if name == "http.post":
            self.posts.append(payload)
            return dict(self.apply_reply)
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

    assert out["ok"] and out["proposalId"] == "prop1"
    assert out["items"] == 3 and out["errors"] == 0
    assert out["tally"] == {"enrich": 2, "new": 1, "redundant": 1}
    assert out["proposalLink"] == "any://s1/prop1"

    items = [item_of(m) for m in fake.modifies]
    assert [m["dataset"] for m in fake.modifies] == \
        ["enrich_proposal_items"] * 3

    # u1: property enrichment; hallucinated "bogus" block dropped
    assert items[0]["outcome"] == "enrich"
    assert items[0]["source"] == "any://s1/tr1#b1"
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
    # u3: conflict demoted to enrich for review; multi-block source
    assert items[2]["outcome"] == "enrich"
    assert items[2]["source"] == "any://s1/tr1#b1,b2"
    assert items[2]["text"] == "Deadline moved to June"

    # proposal object: typed + named after the transcript, body written
    assert fake.created[0]["types"] == ["enrich_proposal"]
    name = fake.created[0]["initialProperties"]["any"]["name"]
    assert name == "Enrichment proposal — Weekly sync"
    assert fake.markdowns[0]["objectId"] == "prop1"
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


def test_apply_wraps_the_server_endpoint():
    fake = happy_fake()
    fake.apply_reply = {"status": 200, "body": json.dumps(
        {"created": 1, "propertiesSet": 2, "enrichedDataWritten": 3,
         "proposalDeleted": True, "failures": ["one bad item"]})}
    out = load(fake).apply("s1", "prop1")
    assert out == {"ok": True, "proposalId": "prop1", "created": 1,
                   "propertiesSet": 2, "enrichedDataWritten": 3,
                   "proposalDeleted": True, "failures": ["one bad item"]}
    assert fake.posts[0]["url"] == \
        "http://any.test/v1/spaces/s1/enrich/apply"
    assert fake.posts[0]["json"] == {"proposalId": "prop1"}


def test_apply_maps_the_404_envelope():
    fake = happy_fake()
    fake.apply_reply = {"status": 404, "body": json.dumps(
        {"error": {"code": "enrich.empty_proposal",
                   "message": "no items in proposal"}})}
    out = load(fake).apply("s1", "gone")
    assert out["ok"] is False
    assert "enrich.empty_proposal" in out["error"]
    assert "404" in out["error"]
