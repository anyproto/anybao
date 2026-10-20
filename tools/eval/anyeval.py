#!/usr/bin/env python3
"""anyeval — ad-hoc before/after eval harness (dev-space task E13).

Runs a fixed question set against a live rig (any server + anyrt
serve), one wiped-clean conversation per question, and records per
run: toolcalls (turns, cells, error records, help() lookups), tokens
(in/out/cacheRead/cacheWrite, duration) and an LLM judge verdict on
the final answer + cell transcript. `compare` diffs two result files.

Usage (rig running per docs/testing-agent-changes.md):
  python3 tools/eval/anyeval.py run --label baseline \\
      --questions tools/eval/questions/anyv1.jsonl [--filter q01 q02] \\
      [--repeat 1] [--no-judge] [--addr http://127.0.0.1:7009] \\
      [--bao bao] [--traces traces-prod-user3] \\
      [--secrets-file configs/.connectors.env]
  python3 tools/eval/anyeval.py compare tools/eval/results/baseline.jsonl \\
      tools/eval/results/candidate.jsonl

Everything goes through the effect boundary: wipe/send, the trace
reader and the judge are scratch guest programs run by `anyrt run`, so
the eval itself leaves traces. A conversation's trace lives in the
serve's any local store, scoped to its bao space (ADR-023 §1): the
reader finds the new toolcaller run with `effects.runs()` and walks
its `llm.chat` spans; `--bao` names that space on `--addr`, `--traces`
is the serve's raw-blob dir (ADR-026 §7). Deliberately ad-hoc — a dev
instrument, no CI wiring.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANYRT = ROOT / "runtime/target/release/anyrt"
RESULTS = Path(__file__).resolve().parent / "results"

# guest helper: wipe the target chat's messages and its log child's
# agentlog datasets (ADR-017: agent_turns/agent_chunks live on the
# `bao/log/v1` child, never on the chat), then send the question as the
# user (no `agent` field = user input). The flat any@v1 surface
# (ADR-010 §8) resolves the space NAME on every call, so ids never go
# stale.
WIPE_SEND = '''
def main(args):
    a = use("any@v1")  # noqa: F821 - guest global
    space = a.get_space(args["space"])["id"]
    chat = a.general_chat(space)
    log = a.chat_log(space, chat)["objectId"]
    wiped = {}
    for obj, ds in ((chat, "chat_messages"), (log, "agent_turns"),
                    (log, "agent_chunks")):
        ids = [r["id"] for r in a.query(space, obj, ds, limit=2000)
               if r.get("id")]
        if ids:
            r = a.delete_records(space, obj, ds, ids)
            if r.get("rejections"):
                return {"error": f"wipe rejected: {r['rejections']}"}
        wiped[ds] = len(ids)
    a.chat_send(space, chat, {"text": args["text"]})
    return {"ok": True, "wiped": wiped}
'''

# guest judge: one classify-tier llm@v1 call, strict-JSON verdict.
JUDGE = '''
import json  # kernel stdlib allowlist; NOT a module-scope guest global


def main(args):
    chat = use("llm@v1").chat  # noqa: F821 - guest global
    system = (
        "You judge one agent conversation against an expectation. "
        "Reply with EXACTLY one JSON object, no fences, no prose: "
        '{"pass": true|false, "reason": "<one line>"}. '
        "Pass means: the agent exercised the expected method/behavior "
        "without flailing (no repeated guess-retry loops), and the final "
        "answer is faithful to the data it retrieved."
    )
    prompt = (
        f"QUESTION: {args['question']}\\n"
        f"EXPECTATION: {args['expect']}\\n\\n"
        f"CELLS RUN (in order):\\n{args['cells']}\\n\\n"
        f"ERRORS HIT: {args['errors'] or 'none'}\\n\\n"
        f"FINAL ANSWER:\\n{args['answer']}"
    )
    reply = chat([{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
                 system=system, tier="classify", max_tokens=1000)
    text = "".join(p.get("text", "") for p in reply["parts"])
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return {"pass": None, "reason": f"unparseable verdict: {text[:120]}"}
'''

# guest reader over the serve's trace store (ADR-023): `op: "ids"` lists
# the toolcaller run ids the finder knows; `op: "find"` picks the new
# toolcaller run (not in `before`, title = the question) and returns
# its metrics + transcript. A run has a summary only once it finished
# (the sink writes `trace_runs` at finish), so "listed" = "completed".
# Cron programs (extraction/rollup/…) share the store — the program
# filter keeps them out.
READ = '''
def main(args):
    runs = effects.runs("toolcaller", limit=0)  # noqa: F821 - guest global
    if args.get("op") == "ids":
        return {"ids": [r["id"] for r in runs]}
    before = set(args.get("before") or [])
    new = [r for r in runs if r["id"] not in before]
    if not new:
        return {"run": None}
    head = (args.get("question") or "")[:40]
    pick = next((r for r in new if (r.get("title") or "").startswith(head)),
                None) or new[0]
    rid = pick["id"]
    spans = effects.query([  # noqa: F821
        {"$match": {"runId": rid, "kind": "span", "name": "llm.chat",
                    "phase": "end"}},
        {"$project": {"seq": 1}}, {"$sort": {"seq": 1}}])
    usage = {"in": 0, "out": 0, "cacheRead": 0, "cacheWrite": 0}
    cells, help_calls, answer = [], 0, ""
    for sp in spans:
        out = effects.get(sp["seq"], run=rid).get("output") or {}  # noqa: F821
        for k in usage:
            usage[k] += (out.get("usage") or {}).get(k, 0) or 0
        texts, code = [], None
        for p in out.get("parts") or []:
            if p.get("type") == "tool_call":
                code = (p.get("args") or {}).get("code", "")
            elif p.get("type") == "text":
                texts.append(p.get("text", ""))
        if code is not None:
            cells.append(code)
            help_calls += code.count("help(") + code.count("inspect.getdoc")
        if out.get("stop") != "tool" and texts:
            answer = "\\n".join(texts)
    errors = [str(r.get("error"))[:160] for r in effects.query([  # noqa: F821
        {"$match": {"runId": rid, "error": {"$ne": None}}},
        {"$project": {"error": 1}}, {"$sort": {"seq": 1}}])]
    return {"run": rid, "status": pick.get("status"),
            "turns": len(spans), "cells": len(cells),
            "effects": pick.get("effects") or 0,
            "error_records": len(errors), "help_calls": help_calls,
            "duration_ms": pick.get("durationMs") or 0, "usage": usage,
            "_cell_sources": cells, "_errors": errors, "_answer": answer}
'''


def bao_space_id(addr, name):
    """The serve's home space on `addr` — its local store holds the
    traces (ADR-023 §1). A raw id passes through."""
    if "." in name:
        return name
    with urllib.request.urlopen(f"{addr}/v1/spaces", timeout=10) as r:
        body = json.load(r)
    spaces = body.get("spaces") if isinstance(body, dict) else body
    for s in spaces or []:
        if s.get("name") == name and s.get("status", "active") == "active":
            return s["id"]
    sys.exit(f"no active space named {name!r} on {addr} (--bao)")


def anyrt_run(programs, name, args_obj, rig):
    """One scratch guest run against the rig: `rig` = {addr, config,
    traces, secrets} — the JSON config names `bao.space`, so the run's
    trace (and the reader's `effects.*`) land in / read the serve's
    store; `secrets` is the serve's --secrets-file (the judge's LLM
    key — `run` reads no env)."""
    cmd = [str(ANYRT), "run", name, "--programs", str(programs),
           "--addr", rig["addr"], "--config", str(rig["config"]),
           "--traces-dir", str(rig["traces"]),
           "--args", json.dumps(args_obj)]
    if rig.get("secrets"):
        cmd += ["--secrets-file", str(rig["secrets"])]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "{}"
    res = json.loads(line)
    if res.get("status") != "ok":
        raise RuntimeError(f"{name} failed: {res.get('error')} "
                           f"(trace {res.get('traceRef')})\n{out.stderr[-500:]}")
    return res["value"]


def wait_for_run(programs, rig, before, question, timeout_s):
    """Metrics + transcript of the new TOOLCALLER run, polled from the
    store through the reader program until it is listed (= finished)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = anyrt_run(programs, "evalread@v1",
                      {"op": "find", "before": sorted(before),
                       "question": question}, rig)
        if m.get("run"):
            return m
        time.sleep(3)
    raise TimeoutError(f"no completed run after {timeout_s}s — is serve up?")


def cmd_run(a):
    questions = [json.loads(x) for x in
                 Path(a.questions).read_text().splitlines() if x.strip()]
    if a.filter:
        questions = [q for q in questions if q["id"] in a.filter]
    if not questions:
        sys.exit("no questions selected")
    programs = Path(tempfile.mkdtemp(prefix="anyeval-"))
    shutil.copytree(ROOT / "repos/_agent/programs", programs,
                    dirs_exist_ok=True)
    (programs / "evalwipe@v1.py").write_text(WIPE_SEND)
    (programs / "evaljudge@v1.py").write_text(JUDGE)
    (programs / "evalread@v1.py").write_text(READ)
    config = programs / "run-config.json"
    config.write_text(json.dumps({"bao.space": bao_space_id(a.addr, a.bao)}))
    rig = {"addr": a.addr, "config": config, "traces": ROOT / a.traces,
           "secrets": a.secrets_file}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = Path(a.out) if a.out else RESULTS / f"{a.label}.jsonl"
    rows = []
    with open(out_path, "w") as out:
        for q in questions:
            for rep in range(a.repeat):
                before = anyrt_run(programs, "evalread@v1", {"op": "ids"},
                                   rig)["ids"]
                anyrt_run(programs, "evalwipe@v1",
                          {"space": a.space, "text": q["question"]}, rig)
                m = wait_for_run(programs, rig, before, q["question"],
                                 a.timeout)
                run = m["run"]
                verdict = None
                if not a.no_judge:
                    cells_txt = "\n---\n".join(m["_cell_sources"]) or "(none)"
                    verdict = anyrt_run(programs, "evaljudge@v1", {
                        "question": q["question"], "expect": q["expect"],
                        "cells": cells_txt[:6000],
                        "errors": "; ".join(m["_errors"])[:1500],
                        "answer": m["_answer"][:4000]}, rig)
                row = {"id": q["id"], "rep": rep, "question": q["question"],
                       "expect": q["expect"], "run": run,
                       **{k: v for k, v in m.items() if not k.startswith("_")},
                       "verdict": verdict}
                out.write(json.dumps(row) + "\n")
                out.flush()
                rows.append(row)
                v = "-" if verdict is None else \
                    {True: "PASS", False: "FAIL"}.get(verdict.get("pass"), "?")
                print(f"{q['id']} rep{rep}: turns={m['turns']} "
                      f"tok_out={m['usage']['out']} errs={m['error_records']} "
                      f"help={m['help_calls']} {v}  ({run})")
    print(f"\n{len(rows)} rows -> {out_path}")


def _agg(rows):
    n = max(len(rows), 1)
    return {"n": len(rows),
            "turns": sum(r["turns"] for r in rows) / n,
            "out": sum(r["usage"]["out"] for r in rows) / n,
            "cacheRead": sum(r["usage"]["cacheRead"] for r in rows) / n,
            "errs": sum(r["error_records"] for r in rows) / n,
            "help": sum(r["help_calls"] for r in rows) / n,
            "dur_s": sum(r["duration_ms"] for r in rows) / n / 1000,
            "pass": sum(1 for r in rows
                        if (r.get("verdict") or {}).get("pass") is True)}


def cmd_compare(a):
    def load(p):
        rows = [json.loads(x) for x in Path(p).read_text().splitlines()]
        by_id = {}
        for r in rows:
            by_id.setdefault(r["id"], []).append(r)
        return by_id

    left, right = load(a.baseline), load(a.candidate)
    ids = [i for i in left if i in right]
    la, lb = Path(a.baseline).stem, Path(a.candidate).stem
    print(f"| id | turns {la}→{lb} | tok_out | errs | help | dur_s | pass |")
    print("|---|---|---|---|---|---|---|")

    def fmt(x, y, prec=1):
        mark = " ✓" if y < x else (" ✗" if y > x else "")
        return f"{x:.{prec}f}→{y:.{prec}f}{mark}"

    for i in ids:
        gl, gr = _agg(left[i]), _agg(right[i])
        print(f"| {i} | {fmt(gl['turns'], gr['turns'])} "
              f"| {fmt(gl['out'], gr['out'], 0)} "
              f"| {fmt(gl['errs'], gr['errs'])} | {fmt(gl['help'], gr['help'])} "
              f"| {fmt(gl['dur_s'], gr['dur_s'])} "
              f"| {gl['pass']}/{gl['n']}→{gr['pass']}/{gr['n']} |")
    tl = _agg([r for i in ids for r in left[i]])
    tr = _agg([r for i in ids for r in right[i]])
    print(f"| **total** | {fmt(tl['turns'], tr['turns'])} "
          f"| {fmt(tl['out'], tr['out'], 0)} | {fmt(tl['errs'], tr['errs'])} "
          f"| {fmt(tl['help'], tr['help'])} | {fmt(tl['dur_s'], tr['dur_s'])} "
          f"| {tl['pass']}/{tl['n']}→{tr['pass']}/{tr['n']} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a question set, write results jsonl")
    r.add_argument("--label", required=True)
    r.add_argument("--questions", required=True)
    r.add_argument("--filter", nargs="*", help="question ids to run")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--space", default="bao")
    r.add_argument("--addr", default="http://127.0.0.1:7009",
                   help="the rig's any server; its local store holds the traces")
    r.add_argument("--bao", default="bao",
                   help="the serve's home space on --addr (name or id)")
    r.add_argument("--traces", default="traces",
                   help="the serve's raw-blob dir (ADR-026 §7), for blob refs")
    r.add_argument("--secrets-file",
                   help="the serve's secrets file; the judge needs its LLM key")
    r.add_argument("--timeout", type=int, default=240)
    r.add_argument("--no-judge", action="store_true")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("compare", help="diff two result files (markdown)")
    c.add_argument("baseline")
    c.add_argument("candidate")
    c.set_defaults(fn=cmd_compare)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
