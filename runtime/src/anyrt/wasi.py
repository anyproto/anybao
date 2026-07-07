"""WasiEngine — ADR-003: THE engine. wasmtime-py host, componentized
CPython guest (runtime/guest/app.py + runtime/wit/kernel.wit, built to
bin/kernel.wasm by `make kernel`).

Limits (per cell): fuel budget (deterministic, replayable), epoch
deadline (hard wall-clock, ticker thread), store memory cap (guest
MemoryError, kernel survives). interrupt() = deadline-to-now + bump.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import wasmtime.component as wc
from wasmtime import Config, Engine, Store, WasiConfig

from .effects import Broker
from .executor import CellError, CellResult

EPOCH_TICK_S = 0.01  # ticker granularity: 10ms per epoch increment


class _Runtime:
    """Process-wide engine + compiled component (compile once, ~750ms)."""

    _lock = threading.Lock()
    _instances: dict[str, _Runtime] = {}

    def __init__(self, kernel_wasm: str):
        cfg = Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = True
        self.engine = Engine(cfg)
        self.component = wc.Component.from_file(self.engine, kernel_wasm)
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self._ticker.start()

    def _tick(self) -> None:  # pragma: no cover - timing thread
        while True:
            time.sleep(EPOCH_TICK_S)
            self.engine.increment_epoch()

    @classmethod
    def get(cls, kernel_wasm: str) -> _Runtime:
        with cls._lock:
            if kernel_wasm not in cls._instances:
                cls._instances[kernel_wasm] = cls(kernel_wasm)
            return cls._instances[kernel_wasm]


class WasiEngine:
    """One instance per conversation (ADR-003 §2)."""

    def __init__(
        self,
        broker: Broker,
        *,
        kernel_wasm: str | Path,
        fuel_per_cell: int = 5_000_000_000,
        memory_bytes: int = 512 * 1024 * 1024,
        default_timeout_s: float = 60.0,
    ):
        self.broker = broker
        self.fuel_per_cell = fuel_per_cell
        self.default_timeout_s = default_timeout_s
        rt = _Runtime.get(str(kernel_wasm))
        self._rt = rt

        linker = wc.Linker(rt.engine)
        linker.add_wasip2()
        linker.root().add_func("host-effect", self._host_effect)

        self.store = Store(rt.engine)
        wasi = WasiConfig()
        wasi.inherit_stderr()  # guest tracebacks; no fs/env/net granted
        self.store.set_wasi(wasi)
        self.store.set_limits(memory_size=memory_bytes)
        self.store.set_fuel(fuel_per_cell)
        self.store.set_epoch_deadline(1_000_000_000)  # parked until a cell runs

        instance = linker.instantiate(self.store, rt.component)
        run_cell = instance.get_func(self.store, "run-cell")
        reset_ns = instance.get_func(self.store, "reset-ns")
        assert run_cell is not None and reset_ns is not None  # exports per kernel.wit
        self._run_cell = run_cell
        self._reset_ns = reset_ns
        self._interrupted = False

    # -- host side of the ONE channel -------------------------------------
    def _host_effect(self, _store, name: str, payload: str) -> str:
        try:
            output = self.broker.call(name, json.loads(payload))
            return json.dumps({"ok": True, "output": output})
        except Exception as e:
            return json.dumps(
                {"ok": False, "error": {"type": type(e).__name__, "message": str(e)}}
            )

    # -- Executor protocol -------------------------------------------------
    def run_cell(self, code: str, *, cell_id: str, timeout_s: float | None = None) -> CellResult:
        self._interrupted = False
        self.broker.current_cell = cell_id
        timeout = timeout_s or self.default_timeout_s
        self.store.set_fuel(self.fuel_per_cell)
        self.store.set_epoch_deadline(max(1, int(timeout / EPOCH_TICK_S)))
        fuel_before = self.store.get_fuel()
        t0 = time.monotonic()
        try:
            raw = self._run_cell(self.store, code)
            reply = json.loads(raw)
            err = reply.get("error")
            cell_err = None
            if err:
                cell_err = CellError(err["type"], err["message"], err.get("traceback", ""))
            result = CellResult(
                cell_id=cell_id,
                ok=reply["ok"],
                error=cell_err,
                prints=reply.get("prints", []),
                last_value=reply.get("last"),
            )
        except Exception as e:
            # trap: fuel exhausted / epoch deadline / interrupt / guest crash
            kind = "Interrupted" if self._interrupted else type(e).__name__
            msg = str(e).splitlines()[0] if str(e) else kind
            result = CellResult(
                cell_id=cell_id,
                ok=False,
                error=CellError(kind, msg),
                interrupted=True,
            )
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        try:
            fuel_used = fuel_before - self.store.get_fuel()
        except Exception:  # store poisoned by trap
            fuel_used = -1
        result.metrics = {"fuel_used": fuel_used, "duration_ms": result.duration_ms}
        err_rec = None
        if result.error:
            err_rec = {"type": result.error.type, "message": result.error.message}
        self.broker.cell_done(
            cell=cell_id,
            ok=result.ok,
            error=err_rec,
            interrupted=result.interrupted,
            metrics=result.metrics,
        )
        self.broker.current_cell = None
        return result

    def interrupt(self) -> None:
        self._interrupted = True
        self.store.set_epoch_deadline(0)
        self._rt.engine.increment_epoch()

    def reset(self) -> None:
        self._reset_ns(self.store)

    def close(self) -> None:
        pass
