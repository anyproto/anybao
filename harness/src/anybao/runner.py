"""Runner — engine + syscalls assembled into runnable programs.

The host contributes the cage and the syscall surface, nothing else:
builtins (time/random/uuid/env/module.resolve/batch/trace), config,
http (route-classified, credential-injecting), and — for conversations
— a mailbox. A conversation IS a program: `toolcaller@v1.main(args)`
runs the whole loop in the guest; a trigger runs its program's main the
same way. Traces are device-local (ADR-006 §1).
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from anyrt import trace as tr
from anyrt.blobstore import FileSidecarStore
from anyrt.builtin_effects import ModuleResolver, register_builtin_effects
from anyrt.effects import Broker, Registry
from anyrt.wasi import WasiEngine

from .anyclient import AnyClient
from .config import Config, register_config_effect
from .effects_impl import register_http_effects
from .mailbox import Mailbox, register_mailbox_effect
from .routes import Classifier

_HARD_BREAK_POLL_S = 0.2


@dataclass
class ProgramResult:
    status: str            # ok | error
    duration_ms: int
    trace_ref: str
    fuel: int | None = None
    error: str | None = None
    value: dict | None = None   # main()'s return, when it round-trips as JSON


class Runner:
    def __init__(
        self,
        client: AnyClient,
        config: Config,
        *,
        kernel_wasm: str | Path,
        traces_dir: str | Path,
        resolver: ModuleResolver,
        user_space: str,
        env: dict[str, str] | None = None,
        system: str = "",
        agent_name: str = "bao",
        any_base: str | None = None,
        grants=None,
        http_request=None,   # injectable wire (offline e2e); None = real
        clock=None,          # injectable determinism pin
    ):
        self._client = client   # host INFRA only (deploy/watch/failure paths)
        self._config = config
        self._kernel = str(kernel_wasm)
        self._traces_dir = Path(traces_dir)
        self._traces_dir.mkdir(parents=True, exist_ok=True)
        self._resolver = resolver
        self._user_space = user_space
        self._env = env or {}
        self._system = system
        self._agent_name = agent_name
        self._classifier = Classifier(any_base)
        self._grants = grants
        self._http_request = http_request
        self._clock = clock
        self._blobs = FileSidecarStore()

    def _build(self, writer: tr.TraceWriter,
               mailbox: Mailbox | None = None) -> tuple[Broker, WasiEngine]:
        reg = Registry()
        register_builtin_effects(reg, env=self._env, resolver=self._resolver,
                                 clock=self._clock)
        register_config_effect(reg, self._config)
        kw = {"request": self._http_request} if self._http_request else {}
        register_http_effects(reg, secrets=self._config.get,
                              classifier=self._classifier, **kw)
        if mailbox is not None:
            register_mailbox_effect(reg, mailbox)
        broker = Broker(reg, writer, grants=self._grants)
        return broker, WasiEngine(broker, kernel_wasm=self._kernel)

    def _dump_trace(self, writer: tr.TraceWriter, run_id: str) -> None:
        base = self._traces_dir / f"{run_id}.jsonl"   # device-local (ADR-006)
        writer.dump(base)
        self._blobs.put(writer.blobs, base)

    def run_program(self, spec: str, args: dict | None = None,
                    mailbox: Mailbox | None = None, run_meta: dict | None = None,
                    run_id: str | None = None) -> ProgramResult:
        """Run a program's main(args). Args round-trip as JSON (the
        trace constraint) into the guest; the return value rides the
        cell's last-value repr back out."""
        run_id = run_id or ("run_" + uuid.uuid4().hex[:16])
        writer = tr.TraceWriter(run={"id": run_id, "program": spec,
                                     **(run_meta or {})})
        broker, executor = self._build(writer, mailbox=mailbox)
        args_lit = json.dumps(json.dumps(args or {}))
        # main()'s return rides back as the driver cell's LAST PRINT
        # (json — parseable), the raw value stays the last expression
        cell = (f"import json\n_a = json.loads({args_lit})\n"
                f"_r = use({spec!r}).main(_a)\nprint(json.dumps(_r))\n_r")

        stop_watchdog = threading.Event()
        if mailbox is not None:  # hard break = interrupt the cage
            def _watch():
                while not stop_watchdog.wait(_HARD_BREAK_POLL_S):
                    if mailbox.hard_break:
                        executor.interrupt()
                        return
            threading.Thread(target=_watch, daemon=True).start()

        try:
            cr = executor.run_cell(cell, cell_id="main")
        finally:
            stop_watchdog.set()
            self._dump_trace(writer, run_id)
            executor.close()

        value = None
        if cr.ok and cr.prints:
            with contextlib.suppress(Exception):
                value = json.loads(cr.prints[-1].repr)
        return ProgramResult(
            status="interrupted" if cr.interrupted else ("ok" if cr.ok else "error"),
            duration_ms=cr.duration_ms, trace_ref=run_id,
            fuel=cr.metrics.get("fuel_used"),
            error=(f"{cr.error.type}: {cr.error.message}" if cr.error else None),
            value=value if isinstance(value, dict) else None)

    def run_conversation(self, chat_id: str, user_text: str,
                         mailbox: Mailbox | None = None) -> ProgramResult:
        """A conversation is toolcaller@v1 with a mailbox. The failure
        path (trap/interrupt) posts the resolution bubble host-side —
        the one thing the guest can no longer say for itself."""
        run_id = "run_" + uuid.uuid4().hex[:16]
        mailbox = mailbox or Mailbox()  # the drain syscall is always there
        result = self.run_program(
            "toolcaller@v1",
            {"space": self._user_space, "chatId": chat_id, "userText": user_text,
             "system": self._system, "agentName": self._agent_name,
             "traceRef": run_id},
            mailbox=mailbox, run_meta={"chatId": chat_id}, run_id=run_id)
        if result.status != "ok":
            text = "Stopped." if result.status == "interrupted" \
                else f"Something broke mid-run (trace {run_id})."
            with contextlib.suppress(Exception):
                self._client.chat_send(self._user_space, chat_id, {
                    "text": text,
                    "agent": {"name": self._agent_name, "done": True}})
        return result

    def run_program_result(self, spec: str, args: dict | None = None):
        """Adapter for TriggerRuntime's run_program hook → RunResult."""
        from .triggers import RunResult
        pr = self.run_program(spec, args)
        return RunResult(status="ok" if pr.status == "ok" else "error",
                         duration_ms=pr.duration_ms,
                         trace_ref=pr.trace_ref, fuel=pr.fuel, error=pr.error)
