"""Enrich a space from a transcript — sourced facts, user-reviewed.

`propose(space, transcript_id)` drafts an enrich_proposal object
(nothing applied). Stage 2 is YOURS: consolidate by EDITING its items
in place — group facets, set targetObjectId, drop redundant; never
rewrite text/source, that kills provenance. The user reviews the
proposal OBJECT, then `apply(space, proposalId)`. Never apply before
approval. Slow (2 LLM passes); failures return {ok: False, error}."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The anybao respawn of bobrik-watch's meetingEnrich@v1 +
# enrichApply@v1 pair. propose() = the mechanical token-heavy stage 1
# (synthesize block-cited units from editor_blocks → ground against
# the space → reconcile new|enrich|conflict|redundant → persist the
# draft); it never writes to target objects. apply() is the
# deterministic (no-LLM) stage 3, implemented HERE.
#
# THIS PROGRAM OWNS THE ENRICHMENT CONTRACT. The server's built-in
# enriched_data/enrich_proposal types and its bespoke endpoints are
# gone (any 03-api "Enrichment (moved userspace)"); both types are
# plain user types this program ensures per space, and consumers
# (any-ui) discover them by xKey, never by id:
#   enrichments      — ONE hub object per space; its enriched_data
#                      dataset holds every applied fact, joined to the
#                      enriched object by `targetObjectId` (records no
#                      longer ride the target — that needed the old
#                      built-in's multitype attach).
#   enrich_proposal  — one object per proposal run; items in its
#                      enrich_proposal_items dataset:
#   { text, source, outcome: "enrich|new",
#     targetObjectId, targetKind: "collection|property",
#     targetProperty: "<typeXKey>.<propXKey>", value, newType, newName }
# `source` is comma-joined dataset-record URIs, one per cited block
# (`any://o/<space>/<transcript>/editor_blocks/<blockId>,…`; block-less
# = the bare object URI `any://o/<space>/<transcript>`) — provenance
# that survives into enriched_data after the proposal is deleted.
# Canonical grammar: any docs/19-links.md §Fragments (WEB-42); the old
# `#<blockId>,…` fragment form is legacy, read-only, never written.

import contextlib
import json
import re

HUB_TYPE_XKEY = "enrichments"
PROPOSAL_TYPE_XKEY = "enrich_proposal"

# Facts are write-once (delete-and-rewrite, never edit — edits would
# forge provenance); the user may delete one in the UI (deleteBy
# anyone); creator/time are server-stamped; text indexes under the
# generic `basic` scope via the x-search mapping.
ENRICHED_DATA_DATASET = {
    "key": "enriched_data",
    "displayName": "Enriched Data",
    "idRule": "auto",
    "deleteBy": "anyone",
    "search": {"text": "text"},
    "fields": [
        {"key": "text", "kind": "string"},
        {"key": "source", "kind": "string"},
        {"key": "target", "kind": "string"},
        {"key": "value", "kind": "string"},
        {"key": "targetObjectId", "kind": "string"},
        {"key": "createdBy", "stamp": "creator"},
        {"key": "createdAt", "stamp": "createTime"},
    ],
}

# Items are edited by BOTH the agent (consolidation) and the human
# reviewer (any-ui) — every field stays mutable. Proposals are
# scaffolding, deleted on apply; no search mapping, so items never
# leak into recall.
ENRICH_PROPOSAL_ITEMS_DATASET = {
    "key": "enrich_proposal_items",
    "displayName": "Enrich Proposal Items",
    "idRule": "auto",
    "deleteBy": "anyone",
    "fields": [
        {"key": key, "kind": "string", "mutableBy": "any"}
        for key in ("text", "source", "outcome", "targetObjectId",
                    "targetKind", "targetProperty", "value",
                    "newType", "newName")
    ],
}

GROUND_LIMIT = 6
BLOCK_LIMIT = 5000
# output budgets: a transcript's unit list runs long — llm@v1's 4096
# default truncates it to stop="length" and the parse fails closed
EXTRACT_MAX_TOKENS = 32000
RECONCILE_MAX_TOKENS = 24000

EXTRACT_SYS = (
    "You read a meeting transcript and SYNTHESIZE its knowledge into "
    "discrete units. A unit is a decision, a fact about an entity, an "
    "action item, or a relation. A unit may be derived from "
    "scattered/non-contiguous lines, or implied (proposed then agreed). "
    "If something was revised during the meeting, capture only the FINAL "
    "state. Each transcript line is prefixed with its block id in "
    "brackets, e.g. `[Tf1csw3SDTC] text`. Return STRICT JSON: an array "
    'of { "id":"u1", "kind":"decision|fact|task|relation", '
    '"statement":"...", "entities":["Project Phoenix","billing module"], '
    '"sourceBlocks":["Tf1csw3SDTC"], "support":["short verbatim quote"] '
    "}. sourceBlocks = the block ids (copied EXACTLY from the "
    "[brackets]) this unit is derived from (1 or more, may be "
    "non-contiguous). entities = named CONCEPTS / projects / docs / "
    "features this unit is about — NOT individual people or speakers. "
    "Write statement and entities in ENGLISH even if the transcript is "
    "in another language (they search an English-leaning knowledge "
    "base). support = 1-3 short verbatim excerpts (keep original "
    "language). No prose outside the JSON.")

RECONCILE_SYS = (
    "You reconcile synthesized meeting knowledge against a space's "
    "existing objects. For EACH knowledge unit decide one outcome:\n"
    "  new       — no matching object exists; mint one.\n"
    "  enrich    — a matching object exists; add a fact or set a "
    "property.\n"
    "  conflict  — a matching object exists but the transcript "
    "contradicts it.\n"
    "  redundant — already fully known; drop.\n"
    "Use ONLY the provided candidate objects as possible matches; never "
    "invent ids. When uncertain whether a candidate truly matches, "
    "prefer `new` (a duplicate is safer to review than a wrong merge).\n"
    'Return STRICT JSON: { "actions": [ {\n'
    '  "unitId":"u1", "outcome":"new|enrich|conflict|redundant",\n'
    '  "targetObjectId":"<existing id or null>",\n'
    '  "newType":"<xKey for new, else null>", '
    '"newName":"<title for new, else null>",\n'
    '  "update":{ "kind":"propUpdate|bodyUpdate|null", '
    '"field":"<prop name or null>", "value":"<text>" },\n'
    '  "reason":"one line"\n'
    "} ] }\nNo prose outside the JSON.")


def _llm(prompt, system, max_tokens):
    reply = use("llm@v1").chat(  # noqa: F821 - guest global
        [{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
        system=system, tier="codegen", tools=[], max_tokens=max_tokens)
    return " ".join(p["text"] for p in reply["parts"]
                    if p["type"] == "text")


def _parse(text):
    """Tolerant JSON: strip a ``` fence, skip leading prose to the first
    [ or {, tolerate trailing prose (raw_decode), and repair invalid \\
    escapes (markdown-escaped underscores in verbatim transcript quotes
    produce "\\_", which is not legal JSON)."""
    if not text:
        return None
    s = str(text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = re.search(r"[\[{]", s)
    if not start:
        return None
    s = s[start.start():]
    for candidate in (s, re.sub(r'\\(?!["\\/bfnrtu])', "", s)):
        try:
            return json.JSONDecoder().raw_decode(candidate)[0]
        except ValueError:
            continue
    # length-truncated array: drop the incomplete tail element and close
    if s[0] == "[" and "}" in s:
        with contextlib.suppress(ValueError):
            return json.loads(s[:s.rindex("}") + 1] + "]")
    return None


def _cited_blocks(c, space, transcript_id):
    """The transcript's editor_blocks in document order: block-cited
    text ("[blockId] text" per line) + the set of valid block ids."""
    rows = c.query(space, transcript_id, "editor_blocks",
                   sort=["nav.pos"], limit=BLOCK_LIMIT)
    lines, valid = [], set()
    for b in rows:
        bid = b.get("id")
        if not bid:
            continue
        valid.add(bid)
        text = (b.get("text") or "").strip()
        if text:
            lines.append(f"[{bid}] {text}")
    return "\n".join(lines), valid


def _extract_units(transcript, valid_ids):
    """LLM synthesis → knowledge units; one retry on an unparseable
    reply. Hallucinated block citations are dropped against valid_ids
    (None = raw-text mode, nothing to check)."""
    units, raw = None, ""
    for _ in range(2):
        raw = _llm(transcript, EXTRACT_SYS, EXTRACT_MAX_TOKENS)
        units = _parse(raw)
        if isinstance(units, list) and units:
            break
    if not isinstance(units, list) or not units:
        return None, raw
    for i, u in enumerate(units):
        if not u.get("id"):
            u["id"] = f"u{i + 1}"
        blocks = u.get("sourceBlocks") or []
        if valid_ids is not None:
            blocks = [b for b in blocks if b in valid_ids]
        u["sourceBlocks"] = blocks
    return units, raw


def _ground(c, space, units, transcript_id, limit):
    """Per-unit candidates from scope-basic search on the unit's clean
    English statement; the transcript itself is never a candidate.
    Hits arrive title-enriched, so no separate name resolution."""
    grounded = {}
    for u in units:
        probe = u.get("statement") or " ".join(u.get("entities") or [])
        if not probe:
            grounded[u["id"]] = []
            continue
        cands = {}
        hits = c.search(space, probe, scopes=["basic"],
                        limit=limit).get("hits") or []
        for h in hits:
            oid = h.get("objectId")
            if not oid or oid == transcript_id:
                continue
            cand = cands.setdefault(
                oid, {"objectId": oid, "name": h.get("title"),
                      "snippets": []})
            if h.get("data"):
                cand["snippets"].append(str(h["data"])[:160])
        grounded[u["id"]] = list(cands.values())
    return grounded


def _type_catalog(c, space):
    """User types (+ the built-in `page`) as "xKey — name: description"
    lines, so `new` actions name a real type; hidden types stay out."""
    lines = []
    for t in c.list_types(space):
        if t.get("builtIn") and t.get("xKey") != "page":
            continue
        if t.get("hidden") and not t.get("builtIn"):
            continue
        key = t.get("xKey") or t.get("id")
        lines.append(f"{key} — {t.get('name')}: {t.get('description') or ''}")
    return lines


def _ensure_store(c, space):
    """Ensure the userspace enrichment store — both types, their
    datasets, and the per-space hub object — and return the hub's
    object id. Idempotent (create_type/_create_dataset are ensures);
    an existing hub wins and duplicates are never auto-deleted (their
    records live on them)."""
    hub_type = c.create_type(space, {
        "name": "Enrichments", "xKey": HUB_TYPE_XKEY, "hidden": True,
        "description": "Per-space enrichment store: sourced facts in "
                       "the enriched_data dataset, joined to enriched "
                       "objects by targetObjectId."})
    c._create_dataset(space, HUB_TYPE_XKEY, ENRICHED_DATA_DATASET)
    c.create_type(space, {
        "name": "Enrich Proposal", "xKey": PROPOSAL_TYPE_XKEY, "hidden": True,
        "description": "Ephemeral, reviewable enrichment plan: one "
                       "enrich_proposal_items record per proposed "
                       "item; deleted on apply."})
    c._create_dataset(space, PROPOSAL_TYPE_XKEY,
                     ENRICH_PROPOSAL_ITEMS_DATASET)
    hubs = c.query_objects(space,
                           filter={"any.type": hub_type["typeId"]},
                           limit=1)
    if hubs:
        return hubs[0]["id"]
    return c.create_object(space, {
        "type": HUB_TYPE_XKEY, "name": "Enrichments"})["objectId"]


def _source(space, transcript_id, blocks):
    if not transcript_id:
        return ""
    if not blocks:
        return f"any://o/{space}/{transcript_id}"
    return ",".join(f"any://o/{space}/{transcript_id}/editor_blocks/{b}"
                    for b in blocks)


@span(kind="getter")  # noqa: F821 - guest global
def analyze(space, opts=None):
    """The stage-1 analysis core WITHOUT persistence — previews, tests.

    extract → ground → reconcile. opts: {transcriptId (block-cited
    sources) | transcript (raw text, no citations), limit?}. Returns
    the raw report {ok, space, transcriptId, units, grounded, actions,
    tally} — or {ok: False, error}."""
    opts = opts or {}
    anymod = use("any@v1")  # noqa: F821 - guest global
    llmmod = use("llm@v1")  # noqa: F821 - guest global
    if not space:
        return {"ok": False, "error": "space required"}
    try:
        c = anymod
        transcript_id = opts.get("transcriptId")
        valid_ids = None
        if transcript_id:
            transcript, valid_ids = _cited_blocks(c, space, transcript_id)
            if not transcript:
                return {"ok": False, "error":
                        f"transcript {transcript_id} has no editor blocks"}
        else:
            transcript = opts.get("transcript") or ""
        if not transcript:
            return {"ok": False,
                    "error": "transcriptId or transcript required"}

        units, raw = _extract_units(transcript, valid_ids)
        if units is None:
            return {"ok": False, "error": "no knowledge units extracted",
                    "transcriptChars": len(transcript),
                    "rawExtractHead": str(raw)[:600]}

        grounded = _ground(c, space, units, transcript_id,
                           opts.get("limit") or GROUND_LIMIT)
        user = (
            "AVAILABLE TYPES (xKey — name):\n"
            + "\n".join(_type_catalog(c, space))
            + "\n\nKNOWLEDGE UNITS:\n" + json.dumps(units)
            + "\n\nGROUNDED CANDIDATES PER UNIT (keyed by unitId; "
              "existing objects — match a unit against ITS OWN "
              "candidates):\n" + json.dumps(grounded))
        recon = _parse(_llm(user, RECONCILE_SYS,
                            RECONCILE_MAX_TOKENS)) or {}
        actions = recon.get("actions") or []
    except (anymod.AnyError, llmmod.LlmError) as e:
        return {"ok": False, "error": str(e)}

    tally = {}
    for a in actions:
        outcome = a.get("outcome") or "?"
        tally[outcome] = tally.get(outcome, 0) + 1
    return {"ok": True, "space": space, "transcriptId": transcript_id,
            "units": units, "grounded": grounded, "actions": actions,
            "tally": tally}


@span(kind="mutator")  # noqa: F821 - guest global
def propose(space, transcript_id, opts=None):
    """Stage 1: analyze the transcript and persist a DRAFT proposal.

    One enrich_proposal_items record per kept item (`redundant`
    dropped, `conflict` demoted to enrich for review); item shape:
    `{text, source, outcome, targetObjectId, targetKind:
    "collection"|"property", targetProperty: "<typeXKey>.<propXKey>",
    value, newType, newName}`. `opts`: `{"limit": grounding
    candidates per unit (default 6)}`. Returns `{ok: True,
    proposalId, proposalLink, space, transcriptId, items, errors,
    tally}` — nothing written to target objects; you consolidate the
    items next."""
    opts = opts or {}
    anymod = use("any@v1")  # noqa: F821 - guest global
    if not space:
        return {"ok": False, "error": "space required"}
    if not transcript_id:
        return {"ok": False, "error": "transcriptId required"}
    report = analyze(space, {"transcriptId": transcript_id,
                             "limit": opts.get("limit")})
    if not report["ok"]:
        return report

    try:
        c = anymod
        _ensure_store(c, space)  # so any-ui's Apply finds the hub too
        name = "transcript"
        rows = c.query_objects(space, filter={"id": transcript_id}, limit=1)
        if rows and (rows[0].get("any") or {}).get("name"):
            name = rows[0]["any"]["name"]
        prop_name = f"Enrichment proposal — {name}"
        pid = c.create_object(space, {
            "type": "enrich_proposal",
            "initialProperties": {"any": {"name": prop_name}}})["objectId"]
    except anymod.AnyError as e:
        return {"ok": False, "error": f"create proposal failed: {e}"}

    units_by_id = {u["id"]: u for u in report["units"]}
    written, errors, tally = 0, 0, {}
    for act in report["actions"]:
        outcome = act.get("outcome") or "new"
        if outcome == "redundant":
            tally["redundant"] = tally.get("redundant", 0) + 1
            continue
        unit = units_by_id.get(act.get("unitId")) or {}
        update = act.get("update") or {}
        is_prop = update.get("kind") == "propUpdate"
        item = {
            "text": unit.get("statement") or update.get("value") or "",
            "source": _source(space, transcript_id,
                              unit.get("sourceBlocks") or []),
            "outcome": "enrich" if outcome == "conflict" else outcome,
            "targetObjectId": act.get("targetObjectId") or "",
            "targetKind": "property" if is_prop else "collection",
            "targetProperty": (update.get("field") or "") if is_prop else "",
            "value": (update.get("value") or "") if is_prop else "",
            "newType": act.get("newType") or "",
            "newName": act.get("newName") or "",
        }
        ops = [{"type": "$set", "path": k, "value": v}
               for k, v in item.items() if v != ""]
        if not ops:
            continue
        try:
            c.modify(space, {
                "objectId": pid, "dataset": "enrich_proposal_items",
                "records": [{"id": "", "upsert": True, "ops": ops}]})
        except anymod.AnyError:
            errors += 1  # counted loud; the draft continues
            continue
        written += 1
        tally[item["outcome"]] = tally.get(item["outcome"], 0) + 1

    # brief human-readable body (the items dataset is the source of truth)
    with contextlib.suppress(anymod.AnyError):  # body is cosmetic
        c.put_markdown(space, pid, (
            f"# {prop_name}\n\n{written} proposed enrichment items in the "
            "`enrich_proposal_items` dataset. Review/edit the items, then "
            "apply with `enrich.apply`.\n\nSource: "
            f"{_source(space, transcript_id, [])}\n"))

    return {"ok": True, "proposalId": pid,
            "proposalLink": f"any://o/{space}/{pid}", "space": space,
            "transcriptId": transcript_id, "items": written,
            "errors": errors, "tally": tally}


@span(kind="mutator")  # noqa: F821 - guest global

def apply(space, proposal_id):
    """Stage 3: deterministically apply a REVIEWED proposal; deletes it.

    No LLM, client-side (the server built-ins are gone): creates ONE
    object per grouped newType+newName, sets real properties for
    `property` items, and writes an enriched_data provenance record
    onto the space's Enrichments hub (joined by `targetObjectId`) for
    every item; then deletes the proposal object. Returns `{ok: True,
    proposalId, created, propertiesSet, enrichedDataWritten,
    proposalDeleted, failures}` — non-empty `failures` still means the
    rest applied. When EVERY item fails the proposal is KEPT (the
    reviewed items are the only copy) and the result is {ok: False,
    proposalDeleted: False, failures, error}. An empty/deleted/unknown
    proposal → {ok: False, error}."""
    anymod = use("any@v1")  # noqa: F821 - guest global
    if not space:
        return {"ok": False, "error": "space required"}
    if not proposal_id:
        return {"ok": False, "error": "proposalId required"}
    c = anymod
    try:
        items = c.query(space, proposal_id, "enrich_proposal_items",
                        limit=1000)
    except ValueError as e:
        # the key→collection resolution (ADR-027 §2): a deleted or
        # unknown proposal object declares no proposal dataset — the
        # same "nothing to apply" as an empty draft. Any other
        # resolution error (ambiguous declaration, bad space) surfaces.
        if "carries no type declaring" not in str(e):
            return {"ok": False, "proposalId": proposal_id,
                    "error": f"apply failed: {e}"}
        items = []
    except anymod.AnyError as e:
        return {"ok": False, "proposalId": proposal_id,
                "error": f"apply failed: {e}"}
    if not items:
        return {"ok": False, "proposalId": proposal_id,
                "error": "empty proposal — no items (already applied, "
                         "deleted, or unknown)"}
    try:
        hub = _ensure_store(c, space)
    except anymod.AnyError as e:
        return {"ok": False, "proposalId": proposal_id,
                "error": f"enrichment store unavailable: {e}"}

    created, props_set, written, failures = 0, 0, 0, []
    minted = {}  # (newType, newName) -> objectId: grouped facets, ONE object
    for it in items:
        iid = it.get("id") or "?"
        try:
            target = it.get("targetObjectId") or ""
            if not target:
                key = (it.get("newType") or "", it.get("newName") or "")
                if not key[0] or not key[1]:
                    failures.append(
                        f"item {iid}: no target and no newType/newName")
                    continue
                if key not in minted:
                    minted[key] = c.create_object(space, {
                        "type": key[0], "name": key[1]})["objectId"]
                    created += 1
                target = minted[key]
            is_prop = (it.get("targetKind") or "") == "property"
            tprop = (it.get("targetProperty") or "") if is_prop else ""
            value = (it.get("value") or "") if is_prop else ""
            if tprop:
                txk, _, pxk = tprop.partition(".")
                if not txk or not pxk:
                    failures.append(
                        f"item {iid}: bad targetProperty {tprop!r}")
                    continue
                # ADR-022 §2: update_object encodes the reviewed value
                # against the property's definition (dates → instants,
                # option names → keys, object names → links); a value
                # that can't be encoded fails this item, not the batch
                try:
                    c.update_object(space, target, {txk: {pxk: value}})
                except (ValueError, TypeError) as e:
                    failures.append(f"item {iid}: {tprop}: {e}")
                    continue
                props_set += 1
            fact = {"text": it.get("text") or "",
                    "source": it.get("source") or "",
                    "target": tprop, "value": value,
                    "targetObjectId": target}
            c.modify(space, {
                "objectId": hub, "dataset": "enriched_data",
                "records": [{"id": "", "upsert": True, "ops": [
                    {"type": "$set", "path": k, "value": v}
                    for k, v in fact.items() if v != ""]}]})
            written += 1
        except anymod.AnyError as e:
            failures.append(f"item {iid}: {e}")

    if created == 0 and props_set == 0 and written == 0:
        # total failure: the proposal is the only copy of the reviewed
        # items — deleting it here would destroy them for nothing
        return {"ok": False, "proposalId": proposal_id, "created": 0,
                "propertiesSet": 0, "enrichedDataWritten": 0,
                "proposalDeleted": False, "failures": failures,
                "error": "nothing applied — proposal kept for review"}

    deleted = True
    try:
        c.delete_object(space, proposal_id)
    except anymod.AnyError as e:
        deleted = False
        failures.append(f"proposal delete: {e}")
    return {"ok": True, "proposalId": proposal_id, "created": created,
            "propertiesSet": props_set, "enrichedDataWritten": written,
            "proposalDeleted": deleted, "failures": failures}


def main(args):
    """Scratch/cron entry: the analysis core without persistence."""
    return analyze(args.get("space"), args)
