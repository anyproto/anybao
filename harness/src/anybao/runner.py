"""Runner — the program-execution adapter (M4 keystone). Assembles a
WasiEngine + the full effect set + the loop into a RUNNABLE
conversation/program. This is what turns the validated pieces into a
working agent: the watcher fires a conversation, a trigger fires a
program, both flow through here.

A run: fresh TraceWriter → Broker with every effect (builtin + config +
llm + http + chat) → WasiEngine → the loop (conversation) or a single
program cell (trigger) → device-local trace → persist the turn (user
space, ADR-006). The llm transport is injectable so the whole adapter
is testable with a scripted transport (no API key) against the real
wasi guest.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from anyrt import trace as tr
from anyrt.blobstore import FileSidecarStore
from anyrt.builtin_effects import ModuleResolver, register_builtin_effects
from anyrt.effects import Broker, Registry
from anyrt.wasi import WasiEngine

from . import roi
from .anyclient import AnyClient
from .autorecall import AutoRecall
from .config import Config, register_config_effect
from .data_effects import register_data_effects
from .effects_impl import register_chat_effect, register_http_effects
from .history import History, build_turn, raw_tail, render_boot_window
from .llm import http_transport as real_llm_transport
from .llm import register_llm_effect
from .loop import LoopPolicy, Mailbox, Outcome, run_conversation
from .memory import register_memory_effects


@dataclass
class ConversationResult:
    outcome: Outcome
    trace_ref: str


@dataclass
class ProgramResult:
    status: str            # ok | error
    duration_ms: int
    trace_ref: str
    fuel: int | None = None
    error: str | None = None


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
        policy: LoopPolicy | None = None,
        llm_transport=None,
    ):
        self._client = client
        self._config = config
        self._kernel = str(kernel_wasm)
        self._traces_dir = Path(traces_dir)
        self._traces_dir.mkdir(parents=True, exist_ok=True)
        self._resolver = resolver
        self._user_space = user_space
        self._env = env or {}
        self._system = system
        self._agent_name = agent_name
        self._policy = policy or LoopPolicy()
        self._llm_transport = llm_transport or real_llm_transport(self._secret)
        self._blobs = FileSidecarStore()

    def _secret(self, ref: str):
        # credentials/keys resolve INSIDE the boundary (M6 injection later)
        return self._config.get(ref)

    def _build(self, writer: tr.TraceWriter, *, chat_id: str | None) -> tuple[Broker, WasiEngine]:
        reg = Registry()
        register_builtin_effects(reg, env=self._env, resolver=self._resolver)
        register_config_effect(reg, self._config)
        register_llm_effect(reg, transport=self._llm_transport, config=self._config.get)
        register_http_effects(reg)
        register_data_effects(reg, self._client)
        register_memory_effects(reg, self._client, space=self._user_space)
        if chat_id is not None:
            register_chat_effect(reg, self._client, space=self._user_space,
                                 chat_id=chat_id, agent_name=self._agent_name)
        broker = Broker(reg, writer)
        return broker, WasiEngine(broker, kernel_wasm=self._kernel)

    def _dump_trace(self, writer: tr.TraceWriter, run_id: str) -> None:
        base = self._traces_dir / f"{run_id}.jsonl"   # device-local (ADR-006)
        writer.dump(base)
        self._blobs.put(writer.blobs, base)

    def _boot_window(self, hist: History) -> tuple[list[dict], int | None]:
        """Boot context + its raw-tail min seq (the ADR-007 §5 deep-history
        guard boundary). Fail-open: a fresh chat / unreachable history
        must not block the conversation."""
        try:
            turns = list(reversed(hist.recent_turns(200)))  # ascending by seq
            chunks = {}
            for lvl in (1, 2, 3):
                got = list(reversed(hist.chunks_at_level(lvl, 100)))
                if got:
                    chunks[lvl] = got
        except Exception:
            return [], None
        tail = raw_tail(turns)
        min_seq = tail[0].get("seq") if tail else None
        return render_boot_window(raw_turns=turns, chunks_by_level=chunks), min_seq

    def run_conversation(self, chat_id: str, user_text: str,
                         mailbox: Mailbox | None = None) -> ConversationResult:
        run_id = "run_" + uuid.uuid4().hex[:16]
        writer = tr.TraceWriter(run={"id": run_id, "program": "toolcaller", "chatId": chat_id})
        broker, executor = self._build(writer, chat_id=chat_id)
        hist = History(self._client, space=self._user_space, chat_id=chat_id)
        boot_msgs, boot_min_seq = self._boot_window(hist)
        recall = AutoRecall(self._client, self._user_space)
        try:
            outcome = run_conversation(
                user_text, broker=broker, executor=executor,
                system=self._system, policy=self._policy, mailbox=mailbox,
                boot_messages=boot_msgs,
                recall_inject=lambda q: recall.messages_for(q, boot_min_seq=boot_min_seq))
        finally:
            self._dump_trace(writer, run_id)
            executor.close()

        hist.append_turn(
            build_turn(user_text=user_text, outcome=outcome, trace_ref=run_id,
                       from_agent=self._agent_name))
        if recall.last_injected:  # ROI log (ADR-007 §5) — best-effort
            with contextlib.suppress(Exception):
                roi.log_injection(self._client, self._user_space,
                                  recall.last_injected, outcome.replies,
                                  ts=int(time.time()))
        return ConversationResult(outcome, run_id)

    def run_program(self, spec: str, args: dict | None = None) -> ProgramResult:
        """Run a program's main(args) — the trigger execution path.
        Args round-trip as JSON (the trace constraint) into the guest."""
        run_id = "run_" + uuid.uuid4().hex[:16]
        writer = tr.TraceWriter(run={"id": run_id, "program": spec})
        broker, executor = self._build(writer, chat_id=None)
        args_lit = json.dumps(json.dumps(args or {}))
        cell = f"import json\n_a = json.loads({args_lit})\nuse({spec!r}).main(_a)"
        try:
            cr = executor.run_cell(cell, cell_id="trigger")
        finally:
            self._dump_trace(writer, run_id)
            executor.close()
        return ProgramResult(
            status="ok" if cr.ok else "error",
            duration_ms=cr.duration_ms, trace_ref=run_id,
            fuel=cr.metrics.get("fuel_used"),
            error=(f"{cr.error.type}: {cr.error.message}" if cr.error else None))

    def run_program_result(self, spec: str, args: dict | None = None):
        """Adapter for TriggerRuntime's run_program hook → RunResult."""
        from .triggers import RunResult
        pr = self.run_program(spec, args)
        return RunResult(status=pr.status, duration_ms=pr.duration_ms,
                         trace_ref=pr.trace_ref, fuel=pr.fuel, error=pr.error)
