"""Salience decay sweep (cron).

ADR-007 §4.2: mechanism present,
trigger ships DISABLED — activation is gated on its eval (never
validated in production by amemory; metrics-first doctrine).

Pure math, no LLM: salience halves every `halfLifeDays` of idleness
(idle = since modifiedAt, falling back to createdAt — access bumps
touch modifiedAt, so recalled items stay warm). Items at/below `floor`
are left alone (decay compresses the range, it never deletes). args:
{space, halfLifeDays?, floor?, batch?} — the brain resolves
itself (ADR-017: deterministic ids are not passed around).
"""

HALF_LIFE_DAYS = 30
FLOOR = 0.1
BATCH = 200
DAY_S = 86400


def decayed(salience, idle_s, half_life_days):
    return round(salience * 0.5 ** (idle_s / (half_life_days * DAY_S)), 3)


def main(args):
    space = args["space"]
    half_life = args.get("halfLifeDays", HALF_LIFE_DAYS)
    floor = args.get("floor", FLOOR)
    ts = now()  # noqa: F821 - guest global (time effect, recorded)
    c = use("any@v1")  # noqa: F821 - guest global
    brain = c.get_brain(space)["objectId"]
    mem = use("memory@v1").memory(c, space)  # noqa: F821 - guest global
    items = c.query(space, brain, "agent_memory_items",
                    limit=args.get("batch", BATCH))
    swept = updated = 0
    for item in items:
        swept += 1
        salience = item.get("salience")
        if not isinstance(salience, (int, float)) or salience <= floor:
            continue
        touched = ts_s(item.get("modifiedAt")) or ts_s(item.get("createdAt")) or ts  # noqa: F821
        idle_s = max(0, ts - touched)
        new = max(floor, decayed(salience, idle_s, half_life))
        if new < salience:
            mem.evolve(item["id"], salience=new)
            updated += 1
    return {"swept": swept, "updated": updated}
