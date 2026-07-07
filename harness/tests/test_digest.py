from anybao import digest
from anyrt import trace as tr
from anyrt.executor import CellError, CellResult, ValueRef


def _cell(cell_id="c1", **kw):
    return CellResult(cell_id=cell_id, ok=kw.pop("ok", True), **kw)


def test_inline_small_value():
    r = _cell(last_value=ValueRef("42", 2, "int"))
    out = digest.render(r, [])
    assert "Last value: 42" in out


def test_big_value_stubbed_with_selector_and_schema():
    big = ValueRef("x" * 8000, 8000, "list[143 × {id:str, name:str}]")
    r = _cell(prints=[big])
    out = digest.render(r, [], policy=digest.DigestPolicy(inline_token_budget=100))
    assert "8000 bytes" in out
    assert "list[143 × {id:str, name:str}]" in out
    assert 'values.get("c1", 0)' in out
    assert "x" * 8000 not in out  # not inlined


def test_side_effects_grouped_with_pointer():
    w = tr.TraceWriter(run={"id": "d"})
    k = tr.input_key("http.get", {})
    for _ in range(3):
        w.effect(effect="http.get", cell="c1", input={}, key=k, output={}, meta={"class": "read"})
    w.effect(effect="objects.create", cell="c1", input={}, key=k, output={},
             meta={"class": "mutate"})
    out = digest.render(_cell(last_value=ValueRef("1", 1, "int")), w.records)
    assert "http.get ×3" in out
    assert "mutate objects.create" in out
    assert 'effects.of("c1")' in out


def test_teaching_hint_on_repeated_effect():
    w = tr.TraceWriter(run={"id": "h"})
    k = tr.input_key("http.get", {})
    for _ in range(5):
        w.effect(effect="http.get", cell="c1", input={}, key=k, output={}, meta={"class": "read"})
    hints = digest.teaching_hints(_cell(), w.records)
    assert any("get_many" in h for h in hints)


def test_error_and_interrupted():
    r = _cell(ok=False, error=CellError("ValueError", "boom", "trace…"))
    assert "Error: ValueError: boom" in digest.render(r, [])
    ri = CellResult(cell_id="c1", ok=True, interrupted=True)
    assert "interrupted" in digest.render(ri, [])
