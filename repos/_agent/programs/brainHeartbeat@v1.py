"""Brain heartbeat v0: exclusions probe and cadence-gap signal."""

# ADR-033. A cron job the brain's setup run writes (every 600 s), with
# args {space, hub, datasets} naming Bao's space, the brain hub object
# and its store collections. v0 does two things: it refuses to run
# without a readable exclusion list (fail closed — later phases read
# mail, and the list says whose mail must never be read), and it writes
# a `heartbeat_gap` signal when its own last tick is too old. A run's
# verdict is whether it RAISED: every refusal is a guest-defined
# exception, so `errorType` names it and the breaker counts it.

kit = use("brainKit@v1")  # noqa: F821 - guest global

GAP_S = 1200
SUBJECT = "agent:brainHeartbeat@v1"


class unconfigured(Exception):
    """The job's args do not name the brain hub: the brain is not set up."""


def _signal(space, hub, collection, tick, kind, decision, reason):
    kit.upsert(space, hub, collection, f"{int(tick)}-{kind}", {
        "tick": tick, "kind": kind, "subject": SUBJECT, "decision": decision,
        "reason": reason}, f"writing a {kind} signal")


def main(args):
    parsed = kit.job_args(args, "signals")
    if parsed is None:
        raise unconfigured("the brain heartbeat needs space, hub and datasets — "
                           "set the brain up first")
    space, hub, datasets = parsed
    tick = now()  # noqa: F821 - guest global
    problem = kit.exclusions_problem(
        kit.hub_record(space, hub, datasets["config"], "exclusions"))
    if problem is not None:
        _signal(space, hub, datasets["signals"], tick, "exclusions_unreadable",
                "refused", problem)
        raise kit.exclusions_unreadable(problem)
    last = (kit.config_value(space, hub, datasets, "heartbeat") or {}).get("lastTickAt")
    gap = None
    if isinstance(last, (int, float)) and tick - last > GAP_S:
        gap = tick - last
        _signal(space, hub, datasets["signals"], tick, "heartbeat_gap", "none",
                f"no tick for {int(gap)}s")
    kit.write_config(space, hub, datasets, "heartbeat", {"lastTickAt": tick})
    return {"ok": True, "gap_s": gap}
