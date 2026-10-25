"""Brain heartbeat v0: exclusions probe and cadence-gap signal."""

# ADR-033. A cron job the brain's setup run writes (every 600 s), with
# args {space, hub, datasets} naming Bao's space, the brain hub object
# and its store collections. v0 does two things: it refuses to run
# without a readable exclusion list (fail closed — later phases read
# mail, and the list says whose mail must never be read), and it writes
# a `heartbeat_gap` signal when its own last tick is too old. A run's
# verdict is whether it RAISED: every refusal is a guest-defined
# exception, so `errorType` names it and the breaker counts it.

_any = use("any@v1")  # noqa: F821 - guest global

GAP_S = 1200
SUBJECT = "agent:brainHeartbeat@v1"


class unconfigured(Exception):
    """The job's args do not name the brain hub: the brain is not set up."""


class exclusions_unreadable(Exception):
    """The exclusion list is missing or not in its version-1 shape."""


def _config_record(space, hub, collection, name):
    # a SHARED dataset: an `id` filter compares "<objectId>/<recordId>"
    rows = _any.query(space, hub, collection, filter={"id": f"{hub}/{name}"}, limit=1)
    return rows[0] if rows else None


def _exclusions_problem(rec):
    """Why the exclusion record is unusable, or None when it is fine."""
    if rec is None:
        return "no exclusion list"
    value = rec.get("value")
    if not isinstance(value, dict):
        return "the exclusion list is not an object"
    if value.get("version") != 1:
        return f"exclusion list version {value.get('version')!r}, expected 1"
    for field in ("personIds", "emails", "domains"):
        if not isinstance(value.get(field), list):
            return f"exclusion list field {field!r} is not a list"
    return None


def _signal(space, hub, collection, tick, kind, decision, reason):
    _any.upsert_record(space, hub, collection, f"{int(tick)}-{kind}", {
        "tick": tick, "kind": kind, "subject": SUBJECT, "decision": decision,
        "reason": reason})


def main(args):
    args = args or {}
    space, hub = args.get("space"), args.get("hub")
    datasets = args.get("datasets") if isinstance(args.get("datasets"), dict) else {}
    if not space or not hub or not datasets.get("config") or not datasets.get("signals"):
        raise unconfigured("the brain heartbeat needs space, hub and datasets — "
                           "set the brain up first")
    tick = now()  # noqa: F821 - guest global
    problem = _exclusions_problem(
        _config_record(space, hub, datasets["config"], "exclusions"))
    if problem is not None:
        _signal(space, hub, datasets["signals"], tick, "exclusions_unreadable",
                "refused", problem)
        raise exclusions_unreadable(problem)
    beat = _config_record(space, hub, datasets["config"], "heartbeat")
    last = ((beat or {}).get("value") or {}).get("lastTickAt")
    gap = None
    if isinstance(last, (int, float)) and tick - last > GAP_S:
        gap = tick - last
        _signal(space, hub, datasets["signals"], tick, "heartbeat_gap", "none",
                f"no tick for {int(gap)}s")
    _any.upsert_record(space, hub, datasets["config"], "heartbeat",
                       {"value": {"lastTickAt": tick}})
    return {"ok": True, "gap_s": gap}
