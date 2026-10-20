#!/usr/bin/env python3
"""anyeval — ad-hoc before/after eval harness (dev-space task E13).

Runs a fixed question set against a live rig (any server + anyrt
serve), one wiped-clean conversation per question, and records per
run: toolcalls (turns, cells, error records, help() lookups), tokens
(in/out/cacheRead/cacheWrite, duration) and an LLM judge verdict on
the final answer + cell transcript. `compare` diffs two result files.
Each conversation is read from the --addr server's trace store
(ADR-023) through `anyrt trace ls/show --json`.

Usage (rig running per docs/testing-agent-changes.md; `make runtime`):
  python3 tools/eval/anyeval.py run --label baseline \\
      --questions tools/eval/questions/anyv1.jsonl [--filter q01_list_spaces] \\
      [--repeat 1] [--no-judge] [--addr http://127.0.0.1:7134] [--space bao]
  python3 tools/eval/anyeval.py compare tools/eval/results/baseline.jsonl \\
      tools/eval/results/candidate.jsonl

Everything goes through the effect boundary: wipe/send and the judge
are scratch guest programs run by `anyrt run`, so the eval itself
leaves traces. Deliberately ad-hoc — a dev instrument, no CI wiring.
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
# agentlog datasets (ADR-017), then send the question as the user (no
# `agent` field = user input). Discovers the space/chat by name so ids
# never go stale.
WIPE_SEND = '''
def main(args):
    a = use("any@v1")  # noqa: F821 - guest global
    space = args["space"]
    chat = a.general_chat(space)
    log = a.chat_log(space, chat)["objectId"]
    for obj, ds in ((chat, "chat_messages"), (log, "agent_turns"),
                    (log, "agent_chunks")):
        ids = [r["id"] for r in a.query(space, obj, ds, limit=2000)
               if r.get("id")]
        if ids:
            r = a.delete_records(space, obj, ds, ids)
            if r.get("rejections"):
                raise RuntimeError(f"wipe rejected: {r['rejections']}")
    a.chat_send(space, chat, {"text": args["text"]})
    return {"ok": True}
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


def space_id(addr, name):
    with urllib.request.urlopen(f"{addr}/v1/spaces") as r:
        spaces = json.load(r)["spaces"]
    for s in spaces:
        if s.get("name") == name and s.get("status") == "active":
            return s["id"]
    sys.exit(f"no active space {name!r} on {addr}")


def anyrt_run(work, name, args_obj, addr):
    """`work` holds programs/ and the --config map; its bao.space is
    where `anyrt run` lands the eval's own traces (ADR-023 §1)."""
    cmd = [str(ANYRT), "run", name, "--programs", str(work / "programs"),
           "--config", str(work / "config.json"),
           "--traces-dir", str(ROOT / "traces"),
           "--args", json.dumps(args_obj), "--addr", addr]
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "{}"
    res = json.loads(line)
    if res.get("status") != "ok":
        raise RuntimeError(f"{name} failed: {res.get('error')} "
                           f"(trace {res.get('traceRef')})\n{out.stderr[-500:]}")
    return res["value"]


def anyrt_trace(*args):
    out = subprocess.run([str(ANYRT), "trace", *args, "--json"],
                         capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"anyrt trace {args[0]} failed:\n{out.stderr[-500:]}")
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]


def toolcaller_runs(addr, space):
    """Finished toolcaller runs, newest first. Cron programs
    (extraction/rollup/…) share the store and outnumber conversations;
    an unfiltered newest run is usually one of them."""
    return anyrt_trace("ls", "--addr", addr, "--space", space,
                       "--program", "toolcaller", "-n", "20")


def parse_trace(records):
    """Metrics + transcript from one toolcaller run's records."""
    turns, cells, errors, help_calls = 0, [], [], 0
    usage = {"in": 0, "out": 0, "cacheRead": 0, "cacheWrite": 0}
    effects = duration_ms = 0
    answer = ""
    for rec in records:
        if rec.get("error") is not None:
            errors.append(str(rec["error"])[:160])
        kind = rec.get("kind")
        if kind == "effect":
            effects += 1
        elif kind == "cell":
            duration_ms = (rec.get("metrics") or {}).get("duration_ms", 0)
        elif kind == "span" and rec.get("name") == "llm.chat" \
                and rec.get("phase") == "end":
            turns += 1
            out = rec.get("output") or {}
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
                answer = "\n".join(texts)
    return {"turns": turns, "cells": len(cells), "effects": effects,
            "error_records": len(errors), "help_calls": help_calls,
            "duration_ms": duration_ms, "usage": usage,
            "_cell_sources": cells, "_errors": errors, "_answer": answer}


def wait_for_run(addr, space, before, timeout_s):
    """Id of a toolcaller run not in `before` that has finished — a run
    gets its summary row (and so an `ls --json` row) only when it ends."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for row in toolcaller_runs(addr, space):
            if row["id"] not in before:
                return row["id"]
        time.sleep(2)
    raise TimeoutError(f"no completed run after {timeout_s}s — is serve up?")


def cmd_run(a):
    questions = [json.loads(x) for x in
                 Path(a.questions).read_text().splitlines() if x.strip()]
    if a.filter:
        questions = [q for q in questions if q["id"] in a.filter]
    if not questions:
        sys.exit("no questions selected")
    work = Path(tempfile.mkdtemp(prefix="anyeval-"))
    programs = work / "programs"
    shutil.copytree(ROOT / "repos/_agent/programs", programs)
    (work / "config.json").write_text(
        json.dumps({"bao.space": space_id(a.addr, a.space)}))
    (programs / "evalwipe@v1.py").write_text(WIPE_SEND)
    (programs / "evaljudge@v1.py").write_text(JUDGE)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = Path(a.out) if a.out else RESULTS / f"{a.label}.jsonl"
    rows = []
    with open(out_path, "w") as out:
        for q in questions:
            for rep in range(a.repeat):
                before = {r["id"] for r in toolcaller_runs(a.addr, a.space)}
                anyrt_run(work, "evalwipe@v1",
                          {"space": a.space, "text": q["question"]},
                          addr=a.addr)
                run = wait_for_run(a.addr, a.space, before, a.timeout)
                m = parse_trace(anyrt_trace("show", run, "--addr", a.addr,
                                            "--space", a.space))
                verdict = None
                if not a.no_judge:
                    cells_txt = "\n---\n".join(m["_cell_sources"]) or "(none)"
                    verdict = anyrt_run(work, "evaljudge@v1", {
                        "question": q["question"], "expect": q["expect"],
                        "cells": cells_txt[:6000],
                        "errors": "; ".join(m["_errors"])[:1500],
                        "answer": m["_answer"][:4000]}, addr=a.addr)
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
    r.add_argument("--addr", default="http://127.0.0.1:7134")
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
