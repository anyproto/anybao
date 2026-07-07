"""Executor protocol + CellResult (ADR-003) and the FakeExecutor test
double (canned results; executes nothing)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CellError:
    type: str
    message: str
    traceback_str: str = ""


@dataclass
class ValueRef:
    """A printed value or last-expression value (ADR-003 §4). `repr`,
    `size`, `schema` come from the guest; the digest decides
    inline-vs-stub, the full object stays in the guest value store
    (values.get)."""

    repr: str
    size: int
    schema: str


@dataclass
class CellResult:
    cell_id: str
    ok: bool
    error: CellError | None = None
    prints: list[ValueRef] = field(default_factory=list)
    last_value: ValueRef | None = None
    duration_ms: int = 0
    interrupted: bool = False
    metrics: dict = field(default_factory=dict)


class Executor(Protocol):
    def run_cell(
        self, code: str, *, cell_id: str, timeout_s: float | None = None
    ) -> CellResult: ...
    def interrupt(self) -> None: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


class FakeExecutor:
    """Test double: scripted CellResults in order. Not an engine."""

    def __init__(self, results: list[CellResult]):
        self._results = list(results)
        self.calls: list[str] = []
        self.reset_count = 0
        self.interrupted = False

    def run_cell(self, code: str, *, cell_id: str, timeout_s: float | None = None) -> CellResult:
        self.calls.append(code)
        if not self._results:
            raise AssertionError("FakeExecutor exhausted")
        r = self._results.pop(0)
        r.cell_id = cell_id
        return r

    def interrupt(self) -> None:
        self.interrupted = True

    def reset(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        pass
