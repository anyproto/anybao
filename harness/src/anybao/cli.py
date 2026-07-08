"""anybao CLI — operational entry points.

`llm-seed` is the docs/llm-fixtures.md "one real call" step: run one
live `llm.chat` through the REAL transport and dump the raw provider
response to a fixture. The response carries no secrets; the request
(which does) is never written. After seeding, CI needs no API key —
adapter translation tests read the fixture.

`serve` is the composition — the bobrik-watch successor: ensure space +
chat, deploy programs/skills, wire Runner + Watcher (trigger #1) +
TriggerRuntime + control API, subscribe to chat_messages, respond. The
side-by-side cutover gate runs THIS against the old binary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from .llm import ADAPTERS, http_transport

FIXTURES_DIR = Path("harness/tests/fixtures")


def llm_seed(args: argparse.Namespace) -> int:
    key = os.environ.get(args.api_key_env, "")
    if not key:
        print(f"error: ${args.api_key_env} is not set", file=sys.stderr)
        return 2
    prov = {"provider": args.provider, "model": args.model,
            "base_url": args.base_url, "api_key_ref": args.api_key_env}
    adapter = ADAPTERS[args.provider]()
    req = adapter.build_request(
        [{"role": "user", "parts": [{"type": "text",
                                     "text": "Reply with the single word OK."}]}],
        "", [], args.model)
    raw = http_transport(os.environ.get)(prov, req)
    # sanity: the adapter must be able to translate what came back
    reply = adapter.parse_response(raw)
    assert reply["parts"], "provider response translated to no parts"

    out = Path(args.out or FIXTURES_DIR / f"llm_{args.provider}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=2) + "\n")
    print(f"seeded {out} (stop={reply['stop']}, "
          f"usage in/out={reply['usage']['in']}/{reply['usage']['out']})")
    return 0


_DEFAULTS = {
    "anthropic": ("claude-haiku-4-5-20251001", "https://api.anthropic.com",
                  "ANTHROPIC_API_KEY"),
    "openai-compat": ("gpt-4o-mini", "https://api.openai.com/v1",
                      "OPENAI_API_KEY"),
}


# --- serve --------------------------------------------------------------------

def bootstrap_config():
    """First-keys bootstrap (M2): env → device-scope secrets + tier
    defaults. AnyConfigStore (agent_config dataset) replaces the dict
    store when config writes need to sync — the cascade is the same."""
    from .config import Config, DictConfigStore
    cfg = Config(DictConfigStore())
    cfg.define("llm.key.anthropic", secret=True)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        cfg.set("llm.key.anthropic", key, scope="device")
    for tier, model in (("codegen", "claude-sonnet-5"),
                        ("classify", "claude-haiku-4-5-20251001")):
        cfg.define(f"llm.tier.{tier}", default={
            "provider": "anthropic", "model": model,
            "base_url": "https://api.anthropic.com",
            "api_key_ref": "llm.key.anthropic"})
    return cfg


def ensure_space(client, name: str) -> str:
    """Adoption rule (ADR-006 §0): name match + active, else create."""
    for sp in client.list_spaces():
        if sp.get("name") == name and sp.get("status", "active") == "active":
            return sp["id"]
    return client._call("POST", "/v1/spaces",
                        {"name": name, "spaceType": "anytype.space"})["id"]


def ensure_chat(client, space: str, name: str) -> str:
    """Find-or-create the watched chat by name. A fresh name = clean v2
    agent_turns/agent_chunks datasets (ADR-006 §0)."""
    rows = client.query_objects(space, filter={"any.name": name}, limit=10)
    for r in rows:
        if "chat" in r:
            return r["id"]
    return client.create_object(space, {
        "types": ["chat"],
        "initialProperties": {"any": {"name": name}}})["objectId"]


def chat_messages(client, space: str, chat_id: str):
    """Post-snapshot chat_messages records (drop-snapshot live feed —
    the same frame contract as the trigger run feed)."""
    for frame in client.subscribe_dataset(space, chat_id, "chat_messages",
                                          sort=["-createdAt"], limit=64):
        event, data = frame["event"], frame["data"]
        if event in ("ready", "snapshot"):
            continue
        if event == "closed":
            return
        if event == "changes" and isinstance(data, list):
            for batch in data:
                for entry in (*batch.get("added", ()), *batch.get("updated", ())):
                    yield {"id": entry.get("id", ""), **(entry.get("doc") or {})}


def serve(args: argparse.Namespace) -> int:
    from .anyclient import AnyClient, sse_http_transport
    from .anyclient import http_transport as any_http
    from .deploy import Deployer
    from .history import rollup_trigger
    from .memory import extraction_trigger, linkgen_trigger
    from .overlays import resolver_from_config
    from .runner import Runner
    from .skills import SkillDeployer, compose_system, load_skills_dir
    from .trigger_control import TriggerService, start_server
    from .triggers import Scheduler, TriggerRuntime, TriggerStore
    from .watch import Watcher

    client = AnyClient(any_http(args.addr), sse_http_transport(args.addr))
    space = ensure_space(client, args.space)
    chat = ensure_chat(client, space, args.chat_name)
    overlay = args.overlay or space  # agent-code target (agent: alias space)

    print(f"deploy → {Deployer(client, space=overlay).deploy_dir(Path(args.programs))}")
    print(f"skills → {SkillDeployer(client, space=overlay).deploy_dir(Path(args.skills))}")

    cfg = bootstrap_config()
    if args.overlay:
        cfg.set("overlays.agent", args.overlay, scope="device")
    resolver = resolver_from_config(client, cfg, current_space=overlay,
                                    private_space=space)
    system = compose_system(load_skills_dir(args.skills))
    # stable block = core skills + tool docs + memory categories (ADR-005 §5)
    import contextlib

    from .skills import memory_categories_section, tool_docs_section
    for section_of, target in ((tool_docs_section, overlay),
                               (memory_categories_section, space)):
        with contextlib.suppress(Exception):  # fresh space = no sections yet
            section = section_of(client, target)
            if section:
                system += "\n\n" + section
    runner = Runner(client, cfg, kernel_wasm=args.kernel, traces_dir=args.traces_dir,
                    resolver=resolver, user_space=space, system=system,
                    agent_name=args.agent_name)

    watcher = Watcher(
        run_conversation=lambda cid, text, mb: _converse(runner, watcher, cid, text, mb),
        start_conversation=lambda cid, text, mb: threading.Thread(
            target=_converse, args=(runner, watcher, cid, text, mb),
            daemon=True).start())

    brain = client.get_brain(space)["objectId"]
    instance = f"anybao-{os.getpid()}"
    sched = Scheduler(instance, now=time.time)
    store = TriggerStore(client, space=space, anchor_object_id=chat)
    runtime = TriggerRuntime(
        sched, lambda t, ev: runner.run_program_result(t.program, t.args),
        record_sink=lambda t, rec: store.record_run(t, rec, ts_ms=int(rec.ts * 1000)),
        boot_time=time.time())
    for t in (rollup_trigger(space=space, chat_id=chat, owner=instance),
              extraction_trigger(space=space, chat_id=chat, owner=instance),
              linkgen_trigger(space=space, brain_id=brain, owner=instance)):
        runtime.add(t)
        store.save(t)
    sched.arm()  # post-sync arming (ADR-006 §4)
    threading.Thread(target=_tick_forever, args=(runtime,), daemon=True).start()

    service = TriggerService(store, owner=instance)
    ctrl = start_server(service, host="127.0.0.1", port=args.control_port)
    print(f"anybao serving space={space} chat={chat} "
          f"control=127.0.0.1:{ctrl.server_address[1]}")

    while True:  # reconnect loop: each feed drops its snapshot, so no replay
        try:
            for record in chat_messages(client, space, chat):
                action = watcher.on_message(chat, record)
                if action == "start":
                    print(f"conversation started: {record.get('text', '')[:60]!r}")
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 - the loop must outlive hiccups
            print(f"feed error: {e}; reconnecting in 2s", file=sys.stderr)
            time.sleep(2)


def _converse(runner, watcher, chat_id, text, mailbox):
    try:
        runner.run_conversation(chat_id, text, mailbox)
    finally:
        watcher.conversation_done(chat_id)


def _tick_forever(runtime, period_s: float = 5.0) -> None:
    while True:
        time.sleep(period_s)
        runtime.tick()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anybao")
    sub = p.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("llm-seed", help="record one real provider call "
                          "as a wire-shape fixture")
    seed.add_argument("--provider", choices=sorted(ADAPTERS), default="anthropic")
    seed.add_argument("--model")
    seed.add_argument("--base-url")
    seed.add_argument("--api-key-env")
    seed.add_argument("--out")
    seed.set_defaults(fn=llm_seed)

    srv = sub.add_parser("serve", help="run the agent against an any server "
                         "(the bobrik-watch successor)")
    srv.add_argument("--addr", default="http://127.0.0.1:7001")
    srv.add_argument("--space", default="bao")
    srv.add_argument("--chat-name", default="general")
    srv.add_argument("--agent-name", default="bao")
    srv.add_argument("--overlay", help="agent-code overlay space id "
                     "(default: deploy into the user space)")
    srv.add_argument("--programs", default="programs")
    srv.add_argument("--skills", default="skills")
    srv.add_argument("--kernel", default="bin/kernel.wasm")
    srv.add_argument("--traces-dir", default="traces")
    srv.add_argument("--control-port", type=int, default=7010)
    srv.set_defaults(fn=serve)

    args = p.parse_args(argv)
    if args.cmd == "llm-seed":
        model, base, env = _DEFAULTS[args.provider]
        args.model = args.model or model
        args.base_url = args.base_url or base
        args.api_key_env = args.api_key_env or env
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
