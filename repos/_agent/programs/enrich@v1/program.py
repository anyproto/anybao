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
# draft); it never writes to target objects. apply() wraps the
# server's deterministic POST /enrich/apply.
#
# Item record shape (server contract — any internal/enrichproposal):
#   { text, source, outcome: "enrich|new",
#     targetObjectId, targetKind: "collection|property",
#     targetProperty: "<typeXKey>.<propXKey>", value, newType, newName }
# `source` is `any://<space>/<transcript>#<blockId>,…` — provenance
# that survives into enriched_data after the proposal is deleted.

import contextlib
import json
import re

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
    """User types (+ the editor builtin) as "xKey — name: description"
    lines, so `new` actions name a real type."""
    lines = []
    for t in c.list_types(space):
        if t.get("builtIn") and t.get("xKey") != "editor":
            continue
        key = t.get("xKey") or t.get("id")
        lines.append(f"{key} — {t.get('name')}: {t.get('description') or ''}")
    return lines


def _source(space, transcript_id, blocks):
    if not transcript_id:
        return ""
    base = f"any://{space}/{transcript_id}"
    return base + "#" + ",".join(blocks) if blocks else base


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
        name = "transcript"
        rows = c.query_objects(space, filter={"id": transcript_id}, limit=1)
        if rows and (rows[0].get("any") or {}).get("name"):
            name = rows[0]["any"]["name"]
        prop_name = f"Enrichment proposal — {name}"
        pid = c.create_object(space, {
            "types": ["enrich_proposal"],
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
            "proposalLink": f"any://{space}/{pid}", "space": space,
            "transcriptId": transcript_id, "items": written,
            "errors": errors, "tally": tally}


@span(kind="mutator")  # noqa: F821 - guest global
def apply(space, proposal_id):
    """Stage 3: apply a REVIEWED proposal server-side; deletes it after.

    No LLM (shared with the any-ui Apply button): creates ONE object
    per grouped newType+newName, sets real properties for `property`
    items, writes an enriched_data provenance record onto every
    target. Returns `{ok: True, proposalId, created, propertiesSet,
    enrichedDataWritten, proposalDeleted, failures}` — non-empty
    `failures` still means the rest applied. Re-apply of a
    deleted/unknown proposal → {ok: False} (404
    enrich.empty_proposal)."""
    if not space:
        return {"ok": False, "error": "space required"}
    if not proposal_id:
        return {"ok": False, "error": "proposalId required"}
    base = effect("config.get",  # noqa: F821 - guest global
                  {"key": "any.base_url"})["value"].rstrip("/")
    reply = effect("http.post", {  # noqa: F821 - guest global
        "url": f"{base}/v1/spaces/{space}/enrich/apply",
        "json": {"proposalId": proposal_id}})
    try:
        r = json.loads(reply.get("body") or "{}")
    except ValueError:
        r = {}
    if reply["status"] >= 400:
        err = r.get("error", {}) if isinstance(r, dict) else {}
        return {"ok": False, "proposalId": proposal_id,
                "error": (f"apply failed: HTTP {reply['status']} "
                          f"{err.get('code', '')} {err.get('message', '')}"
                          ).strip()}
    return {"ok": True, "proposalId": proposal_id,
            "created": r.get("created", 0),
            "propertiesSet": r.get("propertiesSet", 0),
            "enrichedDataWritten": r.get("enrichedDataWritten", 0),
            "proposalDeleted": bool(r.get("proposalDeleted")),
            "failures": r.get("failures") or []}


def main(args):
    """Scratch/cron entry: the analysis core without persistence."""
    return analyze(args.get("space"), args)
