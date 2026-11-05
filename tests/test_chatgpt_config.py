"""Complete config intent through the real guest kernel; synthetic Any boundary."""

import contextlib
import copy
import hashlib
import json
from pathlib import Path

import pytest
from kernelenv import load_kernel

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "docs/fixtures/chatgpt-model-config-v1.json").read_text())
TARGET = "llm.configuration.target"
ABANDONED = "llm.configuration.abandoned"
TIERS = ("codegen", "classify", "vision")
ROW = {
    "provider": "openai-responses",
    "backend": "openai-codex",
    "model": "synthetic",
    "base_url": "https://chatgpt.com/backend-api/codex",
    "api_key_ref": "connector.oauth.openai-codex",
}


class Store:
    def __init__(self, rows=()):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.writes = []
        self.reject = None
        self.reject_record = None
        self.before_write = None
        self.after_write = None
        self.before_read = None

    def bundle_child(self, *_):
        return {"objectId": "synthetic-config"}

    def query(self, _space, _obj, _dataset, **kw):
        assert kw["limit"] == 1025
        assert len(kw["filter"]["$or"]) == 2
        if self.before_read:
            self.before_read(self)
        return copy.deepcopy(list(self.rows.values()))

    def modify(self, _space, batch):
        if self.before_write:
            self.before_write(self, batch)
        self.writes.append(copy.deepcopy(batch))
        if len(self.writes) == self.reject:
            return {"rejections": [{"recordId": "rejected", "reason": "synthetic"}]}
        rows = copy.deepcopy(self.rows)
        rejected = []
        for record in batch["records"]:
            if record["id"] == self.reject_record:
                rejected.append({"recordId": record["id"], "reason": "synthetic"})
                continue
            row = rows.setdefault(record["id"], {"id": record["id"]})
            for op in record["ops"]:
                if op["type"] == "$set":
                    row[op["path"]] = copy.deepcopy(op["value"])
                elif op["type"] == "$addToSet":
                    values = row.setdefault(op["path"], [])
                    if op["value"] not in values:
                        values.append(op["value"])
                else:
                    assert op == {"type": "$unset", "path": "value"}
                    row.pop("value", None)
        self.rows = rows  # one snapshot boundary for the whole accepted batch
        if self.after_write:
            self.after_write(self, batch)
        return {"rejections": rejected}


def module(store, identity="synthetic-apply"):
    counter = 0

    def effect(name, _payload):
        nonlocal counter
        assert name == "uuid4", name
        counter += 1
        return {"hex": identity if counter == 1 else f"{identity}-{counter}"}

    return load_kernel(effect=effect, any_client=store).use("agent:config@v1")


@pytest.mark.parametrize("case", FIXTURE["canonical"], ids=lambda case: case["name"])
def test_shared_decode_then_counted_identity(case):
    cfg = module(Store())
    try:
        encoded = "bao-model-v1\n" + cfg._canonical(json.loads(case["json"]))
    except (ValueError, OverflowError, UnicodeError):
        encoded = None
    assert encoded == case["canonical"]
    identity = None if encoded is None else "v1:" + hashlib.sha256(encoded.encode()).hexdigest()
    assert identity == case["configId"]


def test_complete_batch_partial_rejection_never_replayed_and_fresh_apply_recovers():
    store = Store()
    cfg = module(store)
    store.reject_record = "llm.tier.classify"
    tiers = {tier: ROW for tier in TIERS}
    with pytest.raises(Exception, match="configuration_incomplete"):
        cfg.apply_models("synthetic-space", tiers)
    assert len(store.writes) == 1
    assert [r["id"] for r in store.writes[0]["records"]] == [
        TARGET, ABANDONED, "llm.tier.codegen", "llm.tier.classify", "llm.tier.vision"]
    target = store.rows[TARGET]["value"]
    assert "llm.tier.classify" not in store.rows
    store.reject_record = None
    with pytest.raises(Exception, match="configuration_incomplete"):
        cfg.retry_models("synthetic-space", "synthetic-apply")
    assert len(store.writes) == 1
    assert store.rows[TARGET]["value"] == target
    done = cfg.apply_models("synthetic-space", tiers)
    assert done["applyId"] == "synthetic-apply-2"
    assert done["configuration"] == "matched"
    assert cfg.retry_models("synthetic-space", done["applyId"]) == done
    assert len(store.writes) == 2
    with pytest.raises(Exception, match="superseded"):
        cfg.abandon_models("synthetic-space", "another")
    cfg.abandon_models("synthetic-space", done["applyId"])
    with pytest.raises(Exception, match="configuration_abandoned"):
        cfg.retry_models("synthetic-space", done["applyId"])
    assert json.loads(store.rows[TARGET]["value"])["applyId"] == done["applyId"]
    cfg.apply_models("synthetic-space", tiers)
    assert store.rows[ABANDONED]["value"] == []


def test_ambiguous_complete_batch_reply_uses_readback_without_resending():
    store = Store()

    def lose_reply(_store, batch):
        if batch["records"][0]["id"] == TARGET:
            raise RuntimeError("synthetic lost reply after commit")

    store.after_write = lose_reply
    assert (
        module(store).apply_models("space", {tier: ROW for tier in TIERS})["configuration"]
        == "matched"
    )
    assert len(store.writes) == 1


def test_superseding_apply_after_commit_cannot_receive_any_stale_tier_write():
    store = Store()

    def supersede(store, batch):
        store.after_write = None
        module(store, "newer-explicit-intent").apply_models(
            "space", {tier: {**ROW, "model": "newer"} for tier in TIERS})

    store.after_write = supersede
    with pytest.raises(Exception, match="superseded"):
        module(store).apply_models("space", {tier: ROW for tier in TIERS})
    assert len(store.writes) == 2
    assert {store.rows["llm.tier." + t]["value"]["model"] for t in TIERS} == {"newer"}
    assert json.loads(store.rows[TARGET]["value"])["applyId"] == "newer-explicit-intent"


@pytest.mark.parametrize("cancel_newer", [False, True])
def test_late_abandon_preserves_newer_complete_target_and_its_cancellation(cancel_newer):
    store = Store()
    a, b, c = (module(store, identity) for identity in ("apply-A", "apply-B", "apply-C"))
    a.apply_models("space", {tier: ROW for tier in TIERS})
    newer = {tier: {**ROW, "model": "model-B"} for tier in TIERS}

    def interleave(store, batch):
        assert batch["records"][0]["id"] == ABANDONED
        store.before_write = None
        b.apply_models("space", newer)
        if cancel_newer:
            b.abandon_models("space", "apply-B")

    store.before_write = interleave
    with pytest.raises(Exception, match="configuration_superseded"):
        a.abandon_models("space", "apply-A")
    assert json.loads(store.rows[TARGET]["value"])["applyId"] == "apply-B"
    assert all(store.rows["llm.tier." + tier]["value"] == newer[tier] for tier in TIERS)
    canceled = ("apply-B", "apply-A") if cancel_newer else ("apply-A",)
    assert store.rows[ABANDONED]["value"] == [
        hashlib.sha256(identity.encode()).hexdigest() for identity in canceled]
    if cancel_newer:
        with pytest.raises(Exception, match="configuration_abandoned"):
            b.retry_models("space", "apply-B")
    else:
        assert b.retry_models("space", "apply-B")["configuration"] == "matched"
    result = c.apply_models("space", {tier: ROW for tier in TIERS})
    assert result["configuration"] == "matched"
    assert store.rows[ABANDONED]["value"] == []


def test_retry_is_read_only_even_when_a_new_selection_wins_during_its_read():
    store = Store()
    a, b = module(store, "apply-A"), module(store, "apply-B")
    a.apply_models("space", {tier: ROW for tier in TIERS})

    def interleave(store):
        store.before_read = None
        b.apply_models("space", {tier: {**ROW, "model": "model-B"} for tier in TIERS})

    store.before_read = interleave
    with pytest.raises(Exception, match="configuration_superseded"):
        a.retry_models("space", "apply-A")
    assert len(store.writes) == 2
    assert b.retry_models("space", "apply-B")["configuration"] == "matched"


def test_atomic_batch_has_no_intermediate_target_or_tiers_and_last_commit_wins():
    store = Store()
    a, b = module(store, "apply-A"), module(store, "apply-B")

    def interleave(store, _batch):
        store.before_write = None
        assert not store.rows
        b.apply_models("space", {tier: {**ROW, "model": "model-B"} for tier in TIERS})

    store.before_write = interleave
    a.apply_models("space", {tier: ROW for tier in TIERS})
    assert json.loads(store.rows[TARGET]["value"])["applyId"] == "apply-A"
    assert all(store.rows["llm.tier." + tier]["value"] == ROW for tier in TIERS)
    assert len(store.writes) == 2


@pytest.mark.parametrize("invalid", [None, {}, ["bad"], ["a" * 64] * 2, ["a" * 64] * 1025])
def test_bad_cancellation_metadata_cannot_claim_readiness_or_cancel_another_apply(invalid):
    store = Store()
    cfg = module(store)
    cfg.apply_models("space", {tier: ROW for tier in TIERS})
    store.rows[ABANDONED]["value"] = invalid
    with pytest.raises(Exception, match="invalid_model_cancellation"):
        cfg.retry_models("space", "synthetic-apply")
    with pytest.raises(Exception, match="invalid_model_cancellation"):
        cfg.abandon_models("space", "synthetic-apply")
    assert len(store.writes) == 1
    assert cfg.apply_models("space", {tier: ROW for tier in TIERS})["configuration"] == "matched"


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": "alias", "key": "llm.tier.codegen", "value": ROW}],
        [{"id": TARGET, "key": "foreign", "value": None}],
        [{"id": "llm.tier.codegen", "value": ROW}],
    ],
)
def test_aliases_refuse_explicit_adoption(rows):
    store = Store(rows)
    with pytest.raises(Exception, match="ambiguous_model_identity"):
        module(store).apply_models("space", {tier: ROW for tier in TIERS})
    assert store.writes == []


@pytest.mark.parametrize("invalid", [None, "null", "{}", 1])
def test_invalid_or_absent_intent_cannot_be_retried(invalid):
    store = Store([{"id": TARGET, "key": TARGET, "value": invalid}])
    with pytest.raises(ValueError):
        module(store).retry_models("space", "synthetic-apply")
    assert store.writes == []


@pytest.mark.parametrize("case", FIXTURE["targets"], ids=lambda case: case["name"])
def test_shared_raw_target_fixtures(case):
    cfg = module(Store())
    try:
        cfg._parse_target(case["json"])
        valid = True
    except (ValueError, OverflowError, UnicodeError):
        valid = False
    assert valid == case["valid"]


def test_optional_search_transition_is_in_identity_and_preserves_judge_and_custom_rows():
    judge = {"provider": "typesafe", "model": "jev-synthetic"}
    custom = {"provider": "gemini", "model": "custom", "api_key_ref": "google.key.gemini"}
    store = Store([{"id": "llm.tier.judge", "key": "llm.tier.judge", "value": judge},
                   {"id": "search.provider.deepresearch", "key": "search.provider.deepresearch",
                    "value": custom}])
    cfg = module(store)
    tiers = {tier: ROW for tier in TIERS}
    search = {"websearch": {"provider": "gemini", "model": "synthetic",
                             "base_url": "https://generativelanguage.googleapis.com",
                             "api_key_ref": "google.key.gemini"}}
    done = cfg.apply_models("space", tiers, search)
    target = json.loads(store.rows[TARGET]["value"])
    assert target["search"] == search
    assert store.rows["search.provider.websearch"]["value"] == search["websearch"]
    assert store.rows["search.provider.deepresearch"]["value"] == custom
    assert store.rows["llm.tier.judge"]["value"] == judge
    canonical = "bao-model-v1\n" + cfg._canonical({"tiers": tiers, "search": search})
    assert done["configId"] == "v1:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert done == cfg.retry_models("space", done["applyId"])


@pytest.mark.parametrize("case", FIXTURE["searchCompatibility"], ids=lambda case: case["name"])
def test_readiness_search_contract_matches_actual_guest_acceptance(case):
    from test_deepresearch_module import FAILED, _load
    from test_websearch_module import load

    if case["key"] == "deepresearch":
        guest, calls = _load(case["row"], FAILED)
        guest.research("synthetic-space", "synthetic query")
        accepted = "http.post" in calls
    else:
        guest, calls = load(case["row"], [FAILED])
        with contextlib.suppress(ValueError):
            guest["search"]("synthetic query")
        accepted = any(name == "batch" for name, _ in calls)
    assert accepted == case["compatible"]
