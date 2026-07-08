"""programs/decay@v1 + reflection@v1 + evolution@v1 — the ADR-007 §4.2
mechanisms that ship DISABLED. These tests are their offline evals: the
mechanism gate each must hold before its trigger is ever enabled. Guest
sources run with fake any@v1 / llm@v1 / memory@v1 / recall@v1 modules
injected through the `use` seam."""

import json
from pathlib import Path
from types import SimpleNamespace

from anybao.triggers import decay_trigger, evolution_trigger, reflection_trigger

PROGRAMS_DIR = Path(__file__).resolve().parents[2] / "programs"

DAY = 86400
NOW = 1_700_000_000


def run_main(src_name, fake, args, now=NOW):
    g = {"use": fake.use, "now": lambda: now}
    exec(compile((PROGRAMS_DIR / src_name).read_text(), src_name, "exec"), g)
    return g["main"](args)


def _matches(rec, flt):
    for k, cond in flt.items():
        v = rec.get(k)
        if isinstance(cond, dict):
            if "$gt" in cond and not (v is not None and v > cond["$gt"]):
                return False
            if "$lt" in cond and not (v is not None and v < cond["$lt"]):
                return False
            if "$in" in cond and v not in cond["$in"]:
                return False
        elif v != cond:
            return False
    return True


class FakeGuest:
    """One object answering the whole module surface the sweeps use."""

    def __init__(self, *, items=(), state=(), llm_texts=()):
        self.datasets = {"agent_memory_items": list(items),
                         "agent_job_state": list(state)}
        self.llm_texts = list(llm_texts)
        self.llm_calls = []
        self.evolves = []
        self.saves = []
        self.state_writes = []

    # --- any@v1 client ---
    def query(self, space, object_id, dataset, filter=None, sort=None, limit=None):
        rows = [r for r in self.datasets[dataset] if _matches(r, filter or {})]
        for key in reversed(sort or []):
            rows = sorted(rows, key=lambda r: r[key.lstrip("-")],
                          reverse=key.startswith("-"))
        return rows[:limit] if limit else rows

    def upsert_record(self, space, object_id, dataset, record_id, value):
        self.state_writes.append({"space": space, "object_id": object_id,
                                  "dataset": dataset, "record_id": record_id,
                                  "value": value})
        return {"versionId": "v"}

    # --- llm@v1 ---
    def chat(self, messages, system="", tier="codegen", tools=None):
        self.llm_calls.append({"messages": messages, "system": system,
                               "tier": tier, "tools": tools})
        return {"parts": [{"type": "text", "text": self.llm_texts.pop(0)}],
                "stop": "done", "usage": {"in": 1, "out": 1}}

    # --- memory@v1 ---
    def evolve(self, item_id, **fields):
        self.evolves.append({"item_id": item_id, **fields})
        return {"itemId": item_id}

    def save_with_dedup(self, candidate, recall=None):
        self.saves.append(candidate)
        return {"itemId": "new", "action": "create"}

    def use(self, spec):
        if spec == "any@v1":
            return SimpleNamespace(client=lambda base_url=None: self)
        if spec == "llm@v1":
            return SimpleNamespace(chat=self.chat)
        if spec == "memory@v1":
            return SimpleNamespace(memory=lambda client, space: self)
        if spec == "recall@v1":
            return SimpleNamespace(recall=lambda client, space, **kw: self)
        raise AssertionError(f"unexpected module {spec}")


ARGS = {"space": "s1", "brainId": "brain1"}


# --- decay eval ---------------------------------------------------------------

def test_decay_halves_at_half_life_and_respects_floor():
    items = [
        {"id": "hot", "salience": 0.8, "modifiedAt": NOW},            # idle 0
        {"id": "warm", "salience": 0.8, "modifiedAt": NOW - 30 * DAY},
        {"id": "cold", "salience": 0.8, "createdAt": NOW - 300 * DAY},
        {"id": "floored", "salience": 0.1, "modifiedAt": NOW - 300 * DAY},
        {"id": "unscored", "modifiedAt": NOW - 300 * DAY},
    ]
    fake = FakeGuest(items=items)
    out = run_main("decay@v1.py", fake, ARGS)
    assert out == {"swept": 5, "updated": 2}
    by_id = {e["item_id"]: e for e in fake.evolves}
    assert by_id["warm"]["salience"] == 0.4          # exactly one half-life
    assert by_id["cold"]["salience"] == 0.1          # clamped at the floor
    assert "hot" not in by_id and "floored" not in by_id and "unscored" not in by_id


def test_decay_is_idempotent_at_floor():
    items = [{"id": "a", "salience": 0.1, "modifiedAt": NOW - 999 * DAY}]
    fake = FakeGuest(items=items)
    assert run_main("decay@v1.py", fake, ARGS)["updated"] == 0


# --- reflection eval ----------------------------------------------------------

def stale(iid, category="fact", conf=5, edges=None):
    return {"id": iid, "category": category, "context": f"ctx {iid}",
            "accessCount": 0, "createdAt": NOW - 30 * DAY,
            "confidence": conf, "edges": edges or []}


def test_reflection_synthesizes_insight_with_derived_from_edges():
    verdict = {"insight": {"context": "user consistently prefers X", "body": "b"},
               "contradictions": []}
    fake = FakeGuest(items=[stale("m1"), stale("m2"), stale("m3")],
                     llm_texts=[json.dumps(verdict)])
    out = run_main("reflection@v1.py", fake, ARGS)
    assert out["insights"] == 1 and out["contradictions"] == 0
    saved = fake.saves[0]
    assert saved["category"] == "insight" and saved["source"] == "reflection"
    assert saved["confidence"] == 6
    assert {e["to"] for e in saved["edges"]} == {"m1", "m2", "m3"}
    assert all(e["type"] == "derived_from" for e in saved["edges"])


def test_reflection_contradiction_lowers_both_and_links():
    verdict = {"insight": None,
               "contradictions": [{"a": "m1", "b": "m2"},
                                  {"a": "m1", "b": "ghost"}]}   # ghost dropped
    fake = FakeGuest(items=[stale("m1", conf=7), stale("m2", conf=2), stale("m3")],
                     llm_texts=[json.dumps(verdict)])
    out = run_main("reflection@v1.py", fake, ARGS)
    assert out["contradictions"] == 1 and out["insights"] == 0
    by_id = {e["item_id"]: e for e in fake.evolves}
    assert by_id["m1"]["confidence"] == 5 and by_id["m2"]["confidence"] == 1
    assert by_id["m1"]["edges"] == [{"to": "m2", "type": "contradicts"}]
    assert by_id["m2"]["edges"] == [{"to": "m1", "type": "contradicts"}]


def test_reflection_skips_small_clusters_and_recalled_items():
    items = [stale("m1"), stale("m2"),                       # cluster of 2 < 3
             {**stale("m4"), "accessCount": 3},              # recalled — excluded
             {**stale("m5"), "createdAt": NOW - DAY}]        # too young
    fake = FakeGuest(items=items)
    out = run_main("reflection@v1.py", fake, ARGS)
    assert out == {"clusters": 1, "insights": 0, "contradictions": 0, "errors": 0}
    assert fake.llm_calls == []


# --- evolution eval -----------------------------------------------------------

def linked(iid, ts, edges, tags=None):
    return {"id": iid, "category": "fact", "context": f"ctx {iid}",
            "modifiedAt": ts, "edges": edges, "tags": tags or []}


def test_evolution_refreshes_only_context_and_tags():
    update = {"context": "sharper ctx", "tags": ["a", "b"],
              "confidence": 10, "category": "hacked"}          # extras ignored
    fake = FakeGuest(
        items=[linked("m1", 100, [{"to": "m2", "type": "relates_to"}]),
               linked("m2", 90, [])],
        llm_texts=[json.dumps(update)])
    out = run_main("evolution@v1.py", fake, ARGS)
    assert out == {"swept": 1, "refreshed": 1, "errors": 0}
    assert fake.evolves == [{"item_id": "m1", "context": "sharper ctx",
                             "tags": ["a", "b"]}]
    assert fake.state_writes[-1]["value"] == {"lastModifiedAt": 100}


def test_evolution_null_reply_and_no_change_are_skips():
    fake = FakeGuest(
        items=[linked("m1", 100, [{"to": "m2", "type": "relates_to"}]),
               linked("m2", 90, [{"to": "m1", "type": "relates_to"}])],
        # ascending modifiedAt: m2 answers first (null), m1 second (no change)
        llm_texts=["null",
                   json.dumps({"context": "ctx m1", "tags": []})])  # identical
    out = run_main("evolution@v1.py", fake, ARGS)
    assert out["refreshed"] == 0 and fake.evolves == []


def test_evolution_respects_cursor():
    fake = FakeGuest(
        items=[linked("m1", 100, [{"to": "x", "type": "relates_to"}])],
        state=[{"id": "evolution", "lastModifiedAt": 100}])
    assert run_main("evolution@v1.py", fake, ARGS)["swept"] == 0


# --- the gate itself ----------------------------------------------------------

def test_mechanism_triggers_ship_disabled():
    for factory, kw in ((decay_trigger, {}), (reflection_trigger, {}),
                        (evolution_trigger, {})):
        t = factory(space="s1", brain_id="b1", **kw)
        assert t.enabled is False, f"{t.id} must ship OFF (ADR-007 §4.2)"
        assert t.kind == "cron"
