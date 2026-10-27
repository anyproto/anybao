"""What the brain knows about one person, or across Bao's mail and promises.

`person(person_id)` joins a person's brain columns, their promises and the
latest mail with them; `search(query)` searches objects, mail and promises,
newest first within each. Read before answering about a person or a thread;
quote the `link` so the user can open the source.
"""

__any_category__ = "plugin"  # the AI brain is a plugin of the connectors repo (ADR-033 §1)

# ADR-034 §6. Deterministic: no model. `search` runs one index search PER
# SCOPE — one call with a shared limit lets mail crowd everything else out —
# reads each hit's record for its date, and reranks a scope's hits by
# score × 0.5^(age / half-life); a promise does not decay. Calendar and
# meetings scopes join when their syncs land. Every read honours the
# exclusion list and the blocklist: mail with an excluded or blocklisted
# participant or under a blocklisted label, a promise made to one or read
# from such a message, and an excluded person are left out.
# The hub's stores are shared, so their records are read by
# "<hub>/<recordId>"; mail is per mailbox and read by its plain id.

__any_tool__ = True  # agent-callable (ADR-010 §4)

kit = use("brainKit@v1")  # noqa: F821 - guest global
_any = use("agent:any@v1")  # noqa: F821 - guest global

DAY = 86400
BRAIN_SCOPES = ("basic", "email", "promises")
HALF_LIFE_DAYS = {"basic": 180, "email": 60, "calendar": 30, "meetings": 90}
MAX_PER_SCOPE = 20
MAX_PERSON_ROWS = 50


class unknown_person(Exception):
    """No person with that id in Bao's space."""


class person_excluded(Exception):
    """The user keeps this person out of the brain."""


def _ms_to_s(value):
    return value / 1000 if isinstance(value, (int, float)) else None


def _decayed(score, at, half_life, today):
    if half_life is None or at is None:
        return score
    age_days = max(0.0, (today - at) / DAY)
    return score * 0.5 ** (age_days / half_life)


def _mail_kept_out(row, excl, block):
    if kit.labelled_out(row, block):
        return True
    people = list(row.get("participants") or [])
    people += [a for _, a in kit.addresses(row.get("from"), row.get("to"), row.get("cc"))]
    return any(kit.kept_out(str(a).lower(), excl, block) for a in people)


def _by_record(bao, oid, dataset, rids, shared):
    """{plain record id: record} for one object's dataset, in one query."""
    wanted = [f"{oid}/{r}" if shared else r for r in rids]
    rows = _any.query(bao, oid, dataset, filter={"id": {"$in": wanted}}, limit=len(wanted))
    return {kit.plain_id(r["id"]): r for r in rows}


def _email_hits(bao, hits, ctx):
    out, by_box = [], {}
    for h in hits:
        by_box.setdefault(h.get("objectId"), []).append(h)
    for box, group in by_box.items():
        rows = _by_record(bao, box, "email_messages",
                          [kit.plain_id(h.get("recordId")) for h in group], shared=False)
        for h in group:
            rid = kit.plain_id(h.get("recordId"))
            row = rows.get(rid)
            if row is None or _mail_kept_out(row, ctx["excl"], ctx["block"]):
                continue
            out.append({"title": row.get("subject") or h.get("title"),
                        "text": h.get("data") or row.get("snippet"),
                        "objectId": box, "recordId": rid,
                        "link": kit.mail_link(bao, box, rid),
                        "at": _ms_to_s(row.get("internalDate")),
                        "score": h.get("score") or 0.0})
    return out


def _promise_hits(bao, hits, ctx):
    hub, dataset = ctx["hub"], ctx["datasets"].get("promises")
    if not dataset:
        return []
    rows = _by_record(bao, hub, dataset, [kit.plain_id(h.get("recordId")) for h in hits],
                      shared=True)
    kept_out = kit.promises_kept_out(bao, list(rows.values()), ctx["excl"], ctx["block"])
    out = []
    for h in hits:
        rid = kit.plain_id(h.get("recordId"))
        row = rows.get(rid)
        if row is None or rid in kept_out:
            continue
        out.append({"title": row.get("text"), "text": row.get("quote"), "objectId": hub,
                    "recordId": rid, "link": row.get("source"), "status": row.get("status"),
                    "personId": row.get("personId"),
                    "at": ts_s(row.get("madeAt")),  # noqa: F821 - guest global
                    "score": h.get("score") or 0.0})
    return out


def _object_hits(bao, hits, ctx):
    # a hit may be a person kept out by address or domain, not only by id
    oids = sorted({h["objectId"] for h in hits if h.get("objectId")})
    rows = {r["id"]: r for r in (_any.query_objects(bao, filter={"id": {"$in": oids}},
                                                    limit=len(oids)) if oids else [])}
    out = []
    for h in hits:
        oid = h.get("objectId")
        if not oid or oid in ctx["excl"]["personIds"]:
            continue
        if any(kit.kept_out(e, ctx["excl"], ctx["block"])
               for e in kit.person_emails(rows.get(oid) or {})):
            continue
        out.append({"title": h.get("title"), "text": h.get("data"), "objectId": oid,
                    "recordId": h.get("recordId"), "link": f"any://o/{bao}/{oid}",
                    "at": None, "score": h.get("score") or 0.0})
    return out


_HYDRATE = {"email": _email_hits, "promises": _promise_hits, "basic": _object_hits}


@span("brainRecall.search", kind="getter")  # noqa: F821 - guest global
def search(query, scopes=None, per_scope=5):
    """Search objects, mail and promises, newest first within each source.

    → {groups: [{scope, hits: [{title, text, objectId, recordId, link, at,
    score}]}], skipped: [scope]}; `scopes` defaults to basic, email and
    promises; a scope the index cannot answer is listed in `skipped`."""
    bao = _any.bao_space()
    hub, datasets = kit.ready_hub()
    ctx = {"hub": hub, "datasets": datasets, "excl": kit.exclusions(bao, hub, datasets),
           "block": kit.blocklist(bao, hub, datasets)}
    n = max(1, min(int(per_scope), MAX_PER_SCOPE))
    today = now()  # noqa: F821 - guest global
    groups, skipped = [], []
    for scope in scopes or BRAIN_SCOPES:
        hydrate = _HYDRATE.get(scope)
        if hydrate is None:
            skipped.append(scope)
            continue
        try:
            hits = _any.search(bao, query, scopes=[scope], limit=n * 3).get("hits") or []
        except _any.AnyError:
            skipped.append(scope)
            continue
        found = hydrate(bao, hits, ctx)
        for f in found:
            f["rank"] = _decayed(f["score"], f["at"], HALF_LIFE_DAYS.get(scope), today)
        found.sort(key=lambda f: f["rank"], reverse=True)
        groups.append({"scope": scope, "hits": found[:n]})
    return {"groups": groups, "skipped": skipped}


def _person_row(bao, person_id):
    rows = _any.query_objects(bao, filter={"id": person_id}, limit=1)
    if not rows:
        raise unknown_person(f"no person {person_id!r} in Bao's space")
    return rows[0]


def _mail_with(bao, emails, excl, block, limit):
    mail = []
    if not emails:
        return mail
    for box, _addr in kit.mailboxes(bao):
        for row in _any.query(bao, box, "email_messages",
                              filter={"participants": {"$in": emails}},
                              sort=["-internalDate"], limit=limit):
            if _mail_kept_out(row, excl, block):
                continue
            mail.append({"subject": row.get("subject"), "from": row.get("from"),
                         "snippet": row.get("snippet"),
                         "at": _ms_to_s(row.get("internalDate")),
                         "link": kit.mail_link(bao, box, kit.plain_id(row["id"]))})
    mail.sort(key=lambda m: m["at"] or 0, reverse=True)
    return mail[:limit]


@span("brainRecall.person", kind="getter")  # noqa: F821 - guest global
def person(person_id, limit=20):
    """One person as the brain knows them → {person, facts, proposed, promises, mail}.

    `person` is {id, name, emails, aliases, lastContactAt, mailCount};
    `facts` are their current facts in six groups (tastes, views,
    preferences, personal, work, history), each with its verbatim `quote`
    and `source` link; `proposed` are facts awaiting the user's accept;
    `promises` are what the user promised them, newest first, with `status`
    and `link`; `mail` is the latest mail with them across Bao's mailboxes.
    A person the user keeps out of the brain raises person_excluded."""
    bao = _any.bao_space()
    hub, datasets = kit.ready_hub()
    excl = kit.exclusions(bao, hub, datasets)
    block = kit.blocklist(bao, hub, datasets)
    if person_id in excl["personIds"]:
        raise person_excluded("This person is kept out of the brain.")
    row = _person_row(bao, person_id)
    emails = kit.person_emails(row)
    if any(kit.kept_out(e, excl, block) for e in emails):
        raise person_excluded("This person's address is kept out of the brain.")
    n = max(1, min(int(limit), MAX_PERSON_ROWS))
    bp = row.get("brain_person") or {}
    promises = []
    if datasets.get("promises"):
        rows = _any.query(bao, hub, datasets["promises"], filter={"personId": person_id},
                          sort=["-madeAt"], limit=n)
        kept_out = kit.promises_kept_out(bao, rows, excl, block)
        for p in (r for r in rows if kit.plain_id(r["id"]) not in kept_out):
            promises.append({"id": kit.plain_id(p["id"]), "text": p.get("text"),
                             "quote": p.get("quote"), "status": p.get("status"),
                             "taskId": p.get("taskId"), "link": p.get("source"),
                             "at": ts_s(p.get("madeAt"))})  # noqa: F821 - guest global
    facts, proposed = kit.person_facts(bao, hub, datasets, person_id)
    return {"person": {"id": person_id, "name": (row.get("any") or {}).get("name"),
                       "emails": emails, "aliases": list(bp.get("aliases") or []),
                       "lastContactAt": ts_s(bp.get("lastContactAt")),  # noqa: F821
                       "mailCount": bp.get("mailCount")},
            "facts": facts, "proposed": proposed,
            "promises": promises,
            "mail": _mail_with(bao, emails, excl, block, n)}
