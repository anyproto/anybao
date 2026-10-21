# ADR-014 §7
"""Run a per-item step over many items in hops of N, each hop its own run.

For "do X to each of these K items" when one item costs a model call
or a slow fetch: a cell dies at the run's 20-minute wall limit and
unwritten work is lost. Put the step in a program (programs@v1:
`def step(item, **args)` that WRITES its own result), test it on one
item in a cell, then start() the chain: every hop is a fresh run with
its own budget, the next hop is armed before the work, one progress
bar spans the hops, and the last hop posts a summary into your chat.
"""

# MECHANICS (ADR-014 §7). A hop is a `once` agent_triggers record on
# the agent space's trigger anchor (ADR-006 §4), program this module,
# args = the chain + hop number + the tally so far. The scheduler runs
# due triggers one after another in one thread, after re-reading the
# records, so hop N+1 never starts before hop N returns. Hop N arms
# N+1 BEFORE its work (a hop that dies — fuel, wall limit — still
# leaves the chain alive) and rewrites N+1's args AFTER the work with
# the tally and `prev.stamped: True`. N+1 reading `stamped: False`
# knows N died: its slice counts as unfinished; three dead hops in a
# row stop the chain. Nothing is stored outside the trigger records
# (fired hops stay behind as the audit trail, ADR-006 §4) and the
# fired hop's own record is never edited — an args edit re-arms a
# once (ADR-006 §4 refresh).

__any_tool__ = True  # agent-callable (ADR-010 §4)

import re

_any = use("any@v1")  # noqa: F821 - guest global
_prog = use("agent:progress@v1")  # noqa: F821 - progress interface (ADR-014)

_PROGRAM = "agent:chain@v1"
_MAX_ITEMS = 5000
_MAX_BATCH = 200
_MAX_DEAD_HOPS = 3     # consecutive hops that died before reporting → stop
_ERRORS_KEPT = 20      # per-item errors carried in the tally
_TICK_EVERY_S = 30     # heartbeat: a running bar unseen 45 s expires (ADR-014 §4)
_JOB_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_CHAIN_KEYS = ("job", "program", "method", "items", "args", "batch",
               "space", "agentSpace", "label", "gen")
_EMPTY_TALLY = {"done": 0, "failed": 0, "unfinished": 0, "deadStreak": 0,
                "errors": []}


def _space_id(s):
    # accept any spaceConfig shape, store a plain string (it rides in
    # trigger args and in trigger ids)
    if isinstance(s, dict):
        s = s.get("id") or s.get("spaceId") or ""
    if not s:
        raise ValueError("pass a space id/name string, a list_spaces() row, "
                         "or baoSpaceConfig")
    return s


def _anchor(agent_space):
    # the `bao/triggers/v1` bundle child — the one anchor the runtime
    # polls (ADR-017 §0); resolved via the bundle, never by name
    return _any.bundle_child(agent_space, "bao/v1", "bao/triggers/v1")["objectId"]


def _tid(job, gen, hop):
    return f"chain-{job}-g{gen}-h{hop}"


def _hops(chain):
    n, b = len(chain["items"]), int(chain["batch"])
    return (n + b - 1) // b


def _record(chain, hop, tally, prev):
    return {"kind": "once", "spec": {"at": now()},  # noqa: F821 - a past `at` fires on the next tick
            "program": _PROGRAM, "enabled": True,
            "name": f"{chain['job']} hop {hop}/{_hops(chain)}",
            "args": {**chain, "chain": True, "hop": hop, "tally": tally,
                     "prev": prev}}


def _arm(chain, hop, tally, prev):
    tid = _tid(chain["job"], chain["gen"], hop)
    _any.upsert_record(chain["agentSpace"], _anchor(chain["agentSpace"]),
                       "agent_triggers", tid, _record(chain, hop, tally, prev))
    return tid


def _push_error(tally, err):
    if len(tally["errors"]) < _ERRORS_KEPT:
        tally["errors"].append(err)


def _notify(agent_space, job, text):
    # a VISIBLE `trigger:<job>` chat message (ADR-014 §6): the watcher
    # treats a foreign agent name as input, so the loop reports it with
    # the chat's history in context. Best-effort: never fails the hop.
    try:
        chat = _any.general_chat(agent_space)
        sent = _any.chat_send(agent_space, chat, {
            "text": text, "agent": {"name": f"trigger:{job}", "done": True}})
        return (sent or {}).get("id") or chat
    except Exception as e:  # visible in the trace, never fatal
        print(f"notify failed: {type(e).__name__}: {e}")
        return None


def _finish(chain, hop, tally, stopped=None):
    job, space, total = chain["job"], chain["space"], len(chain["items"])
    summary = (f"{tally['done']}/{total} done, {tally['failed']} failed, "
               f"{tally['unfinished']} unfinished")
    if stopped:
        _prog.fail(space, job, error=stopped, detail=summary)
    else:
        # final counts land in the last frame, THEN done() emits the
        # terminal state (ADR-014 §4: the row lingers ~60 s)
        _prog.tick(space, job, current=total, total=total, detail=summary)
        _prog.done(space, job)
    errs = "; ".join(
        f"{e['item'] if 'item' in e else 'hop ' + str(e.get('hop'))}: {e['error']}"
        for e in tally["errors"][:5])
    text = (f"[trigger: {job}] Chain '{job}' {'STOPPED' if stopped else 'FINISHED'}: "
            f"{summary}." + (f" Stopped: {stopped}." if stopped else "")
            + (f" Errors: {errs}." if errs else "")
            + " Reply with ONE short update to the user; your reply reaches the "
              "chat by itself — do NOT chat_send it, or it posts twice.")
    notified = _notify(chain["agentSpace"], job, text)
    return {"hop": hop, "hops": _hops(chain), "chainDone": True,
            "stopped": stopped, "tally": tally, "notified": notified}


# --- agent surface -----------------------------------------------------------

@span("chain.start", kind="mutator")  # noqa: F821 - guest global
def start(agent_space, job, program, items, method="step", args=None,
          batch=10, space=None, label=None):
    """Arm a chain over `items` → {job, total, batch, hops, gen, triggerId}.

    `agent_space`: your home space (baoSpaceConfig — the hops live on
    its trigger anchor). `job`: a slug (a-z, 0-9, -), the progress
    bar's key. The step runs as `use(program).<method>(item, **args)`:
    a spec that resolves in a cell resolves here; the step WRITES its
    own result per item and raises on failure. `items`: ids or small
    dicts (≤ 5000; they ride in every hop's trigger args). `batch`:
    items per hop (default 10) — keep a hop well under the 20-minute
    run limit (a model call is ~15 s). `space`: the bar's subject
    space (default agent_space). Test the step on one item in a cell
    FIRST: the chain only records a failure. A failed item is counted
    and the hop goes on; a hop that dies before reporting leaves its
    items unfinished, three in a row stop the chain. One bar `job`
    spans the hops; the last hop posts a `trigger:<job>` summary into
    your chat, which you then report. stop() disarms pending hops."""
    agent_space = _space_id(agent_space)
    space = _space_id(space) if space else agent_space
    if not _JOB_RE.match(str(job)):
        raise ValueError("job must be a slug: a-z, 0-9, -, up to 48 chars")
    items = list(items or [])
    if not items:
        raise ValueError("items is empty")
    if len(items) > _MAX_ITEMS:
        raise ValueError(f"at most {_MAX_ITEMS} items per chain — split the list")
    batch = int(batch)
    if not 1 <= batch <= _MAX_BATCH:
        raise ValueError(f"batch must be 1..{_MAX_BATCH}")
    step = getattr(use(program), method, None)  # noqa: F821 - guest global
    if not callable(step):
        raise ValueError(f"{program} has no callable {method!r}")
    chain = {"job": job, "program": program, "method": method, "items": items,
             "args": dict(args or {}), "batch": batch, "space": space,
             "agentSpace": agent_space,
             "label": label or f"{job}: {len(items)} items", "gen": int(now())}  # noqa: F821
    tally = {**_EMPTY_TALLY, "errors": []}
    # the bar exists before any hop runs (ADR-014 §1)
    _prog.start(space, job, chain["label"], total=len(items))
    tid = _arm(chain, 1, tally, None)
    return {"job": job, "total": len(items), "batch": batch,
            "hops": _hops(chain), "gen": chain["gen"], "triggerId": tid}


@span("chain.stop", kind="mutator")  # noqa: F821 - guest global
def stop(agent_space, job):
    """Disarm a chain's pending hops → {stopped, disarmed}.

    The hop running right now finishes its batch; the bar reads
    stopped. Fired hops stay as the audit trail."""
    agent_space = _space_id(agent_space)
    anchor = _anchor(agent_space)
    prefix = f"chain-{job}-g"
    disarmed, space = [], agent_space
    for rec in _any.query(agent_space, anchor, "agent_triggers", limit=500):
        tid = str(rec.get("id") or "")
        if (not tid.startswith(prefix) or not rec.get("enabled", True)
                or rec.get("lastRunAt")):
            continue
        _any.modify(agent_space, {
            "objectId": anchor, "dataset": "agent_triggers",
            "records": [{"id": tid, "ops": [
                {"type": "$set", "path": "enabled", "value": False}]}]})
        disarmed.append(tid)
        space = (rec.get("args") or {}).get("space") or space
    if disarmed:
        _prog.fail(space, job, error="chain stopped",
                   detail=f"{len(disarmed)} pending hop(s) disarmed")
    return {"stopped": bool(disarmed), "disarmed": disarmed}


def main(args):
    # trigger entry (ADR-006 §4): one hop — the previous hop's fate,
    # arm the successor, run the slice, stamp the successor
    chain = {k: args.get(k) for k in _CHAIN_KEYS}
    hop, hops = int(args["hop"]), _hops(chain)
    tally = {**_EMPTY_TALLY, **(args.get("tally") or {})}
    tally["errors"] = list(tally.get("errors") or [])
    job, space, agent_space = chain["job"], chain["space"], chain["agentSpace"]
    items, batch = chain["items"], int(chain["batch"])
    lo = (hop - 1) * batch
    chunk = items[lo:lo + batch]
    prev = args.get("prev")
    if prev and not prev.get("stamped"):
        lost = int(prev.get("count") or 0)
        tally["unfinished"] += lost
        tally["deadStreak"] = int(tally.get("deadStreak") or 0) + 1
        _push_error(tally, {"hop": prev.get("hop"),
                            "error": f"died before reporting; {lost} item(s) unfinished"})
        if tally["deadStreak"] >= _MAX_DEAD_HOPS:
            return _finish(chain, hop, tally,
                           stopped=f"{_MAX_DEAD_HOPS} hops in a row died")
    else:
        tally["deadStreak"] = 0
    next_tid = None
    if hop < hops:
        # BEFORE the work: a death below still leaves the chain alive
        next_tid = _arm(chain, hop + 1, tally,
                        {"hop": hop, "count": len(chunk), "stamped": False})
    step = getattr(use(chain["program"]), chain["method"])  # noqa: F821 - guest global
    done = failed = 0
    last_tick = 0
    for i, item in enumerate(chunk):
        try:
            step(item, **(chain.get("args") or {}))
            done += 1
        except Exception as e:  # one bad item never ends the hop
            failed += 1
            _push_error(tally, {"hop": hop, "item": item,
                                "error": f"{type(e).__name__}: {e}"})
        t = now()  # noqa: F821 - guest global
        if t - last_tick >= _TICK_EVERY_S or i == len(chunk) - 1:
            _prog.tick(space, job, current=lo + i + 1, total=len(items),
                       detail=f"hop {hop}/{hops}")
            last_tick = t
    tally["done"] += done
    tally["failed"] += failed
    tally["deadStreak"] = 0          # this hop reported: the streak is over
    if next_tid is None:
        return _finish(chain, hop, tally)
    # AFTER the work: the successor carries the tally — unless stop()
    # disarmed it meanwhile (a rewrite would re-arm it)
    anchor = _anchor(agent_space)
    live = _any.query(agent_space, anchor, "agent_triggers",
                      filter={"id": next_tid}, limit=1)
    if live and not live[0].get("enabled", True):
        # stop() ran during this hop: the ticks above reopened the bar,
        # so close it again; the chain ends here, no summary message
        _prog.fail(space, job, error="chain stopped",
                   detail=f"stopped after hop {hop}/{hops}: {tally['done']} done, "
                          f"{tally['failed']} failed")
        return {"hop": hop, "hops": hops, "done": done, "failed": failed,
                "armed": next_tid, "stopped": "chain stopped", "tally": tally}
    _any.upsert_record(agent_space, anchor, "agent_triggers", next_tid,
                       _record(chain, hop + 1, tally,
                               {"hop": hop, "count": len(chunk), "stamped": True}))
    return {"hop": hop, "hops": hops, "done": done, "failed": failed,
            "armed": next_tid, "tally": tally}
