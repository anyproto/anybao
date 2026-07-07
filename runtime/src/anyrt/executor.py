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
class CellResult:
    cell_id: str
    ok: bool
    error: CellError | None = None
    prints: list[str] = field(default_factory=list)  # repr strings (M0)
    last_value: str | None = None                    # repr string (M0)
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

    def run_cell(self, code: str, *, cell_id: str, timeout_s: float | None = None) -> CellResult:
        self.calls.append(code)
        if not self._results:
            raise AssertionError("FakeExecutor exhausted")
        r = self._results.pop(0)
        r.cell_id = cell_id
        return r

    def interrupt(self) -> None:  # pragma: no cover
        pass

    def reset(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        pass
