"""M0 exit criterion 2: whole-conversation strict replay + divergence.

The committed fixture (fixtures/golden-conv.jsonl) was recorded by this
same scenario (helpers_golden). Replay answers every effect from the
trace while cells re-execute for real in the wasi guest.
Regenerate intentionally with UPDATE_GOLDEN=1 pytest -k up_to_date.
"""

import copy
import os

import helpers_golden as hg
import pytest
from anyrt import trace as tr
from anyrt.executor import CellError, CellResult, FakeExecutor, ValueRef

needs_kernel = pytest.mark.skipif(
    not hg.KERNEL.exists(), reason="bin/kernel.wasm missing — run `make kernel`"
)

EXPECTED_REPLIES = ["The HN digest is the bigger one."]

VOLATILE = ("durMs", "fuel_used", "duration_ms")


def _normalized(records: list[dict]) -> str:
    recs = copy.deepcopy(records)
    for r in recs:
        for volatile_key in VOLATILE:
            r.get("meta", {}).pop(volatile_key, None)
            r.get("metrics", {}).pop(volatile_key, None)
    return "\n".join(tr.canonical_json(r) for r in recs)


@needs_kernel
def test_golden_fixture_up_to_date():
    replies, writer, _ = hg.run_golden("record")
    assert replies == EXPECTED_REPLIES
    if os.environ.get("UPDATE_GOLDEN"):
        hg.FIXTURE.parent.mkdir(exist_ok=True)
        writer.dump(hg.FIXTURE)
    assert hg.FIXTURE.exists(), "fixture missing — rerun with UPDATE_GOLDEN=1"
    assert _normalized(writer.records) == _normalized(tr.load(hg.FIXTURE)), (
        "recorded scenario drifted from committed fixture — "
        "review, then UPDATE_GOLDEN=1 to accept"
    )


@needs_kernel
def test_golden_replay_strict():
    records = tr.load(hg.FIXTURE)
    replies, _, broker = hg.run_golden("replay", records)
    assert replies == EXPECTED_REPLIES
    assert broker.cursor is not None and broker.cursor.exhausted()  # no missing calls


@needs_kernel
def test_golden_replay_divergence_on_tampered_trace():
    records = tr.load(hg.FIXTURE)
    tampered = copy.deepcopy(records)
    # tamper the recorded llm output: the cell now fetches a different
    # url -> its http.get key won't match the recorded one
    for rec in tampered:
        if rec["kind"] != "effect" or rec["effect"] != "llm.chat":
            continue
        if rec["output"]["stop"] == "tool":
            code = rec["output"]["parts"][1]["args"]["code"]
            rec["output"]["parts"][1]["args"]["code"] = code.replace("hn-digest", "TAMPERED")
    with pytest.raises(tr.DivergenceError):
        hg.run_golden("replay", tampered)


def test_loop_over_fake_executor():
    """Loop logic without any engine (the ADR-003 test double)."""
    from anybao.loop import run_conversation
    from anyrt.effects import Broker

    reg = hg.build_registry(copy.deepcopy(hg.SCRIPTED_REPLIES))
    writer = tr.TraceWriter(run={"id": "fake"})
    broker = Broker(reg, writer)
    fake = FakeExecutor([
        CellResult(
            cell_id="", ok=True,
            prints=[ValueRef("sizes {'hn': 120, 'tc': 36}", 27, "str")],
            last_value=ValueRef("'hn'", 4, "str"),
        ),
    ])
    replies = run_conversation("q", broker=broker, executor=fake)
    assert replies == EXPECTED_REPLIES
    assert fake.calls == [hg.CELL_1]


def test_digest_error_shape():
    from anybao import digest

    d = digest.render(
        CellResult(cell_id="c", ok=False, error=CellError("ValueError", "boom")), []
    )
    assert "Error: ValueError: boom" in d
