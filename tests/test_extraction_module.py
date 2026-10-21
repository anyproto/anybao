"""programs/extraction@v1 — the pure `normalize` step, host-side."""

from pathlib import Path

import pytest

PROGRAMS_DIR = Path(__file__).resolve().parents[1] / "repos" / "_agent" / "programs"
SRC = (PROGRAMS_DIR / "extraction@v1.py").read_text()


def load():
    g = {
        "effect": lambda name, payload: pytest.fail(f"unexpected effect {name!r}"),
        "span": lambda name=None, kind=None: (lambda f: f),
        "use": lambda spec: pytest.fail(f"unexpected use({spec!r})"),
    }
    exec(compile(SRC, "extraction@v1.py", "exec"), g)
    return g


def test_normalize_stamps_the_scanned_chat():
    normalize = load()["normalize"]
    raw = {"category": "fact", "context": " the fact ", "confidence": 9}
    out = normalize(raw, 42, "chat1")
    assert out["chatId"] == "chat1"
    assert out["context"] == "the fact"
    assert out["source"] == "extraction"
    assert out["provenance"] == {"fromSeq": 42}
    # no chat known → the field is absent, not empty
    assert "chatId" not in normalize(raw, 42)
    # the shape allow-list still gates
    assert normalize({"category": "nope", "context": "x"}, 1, "chat1") is None
