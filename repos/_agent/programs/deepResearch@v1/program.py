"""Deep research on ONE question, written into the space as pages.

Slow (30s-3min) and writes multiple objects: one sub-page per
follow-up question plus an overview page linking them — the hub; tell
the user its name when done. Reach for it when the user asks for
research / a report / a deep dive, not for a quick fact (that's
`webSearch@v1`)."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-008 §4, four phases: grounded Gemini call → follow-up
# decomposition via llm@v1 (classify tier, 3-7 questions) → batched
# grounded follow-up calls → write-out through any@v1 (`any://` urls,
# deduped sources; no bookmark/collection objects). The api key never
# enters the guest (credential ref, host-injected — ADR-002).

import json

_SYSTEM = (
    "You are a thorough research assistant. Your task is to research the "
    "given question using Google Search grounding and provide a "
    "comprehensive, well-structured answer.\n\n"
    "Guidelines:\n"
    "- Search broadly — use multiple angles and phrasings\n"
    "- Cite specific facts with the sources you find\n"
    "- Structure the answer with clear headings (## sections)\n"
    "- Include concrete details: numbers, dates, names, comparisons\n"
    "- If sources conflict, note the disagreement\n"
    "- End with a brief summary of key findings"
)
_TIMEOUT_S = 150
_MAX_FOLLOW_UPS = 7


def _provider():
    return effect("config.get", {"key": "search.provider.deepresearch"})["value"]  # noqa: F821


def _request(prov, question):
    url = (prov["base_url"].rstrip("/")
           + "/v1beta/models/" + prov["model"] + ":generateContent")
    user = (f"Research this question thoroughly:\n\n{question}\n\n"
            "Search from multiple angles to build a complete picture. Provide "
            "a detailed, well-organized answer with clear section headings.")
    return {
        "url": url,
        "json": {
            "system_instruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"google_search": {}}],
        },
        "timeout": _TIMEOUT_S,
        "credential": {"ref": prov["api_key_ref"], "header": "x-goog-api-key"},
    }


def _domain(url):
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


def _parse(raw):
    """{ok, answer, sources, queries, tokens} | {ok: False, error}.
    Sources dedup by domain within one response (mirrors bobrik)."""
    if isinstance(raw, dict) and set(raw) == {"error"}:  # batch item failure
        return {"ok": False,
                "error": f"{raw['error']['type']}: {raw['error']['message']}"}
    try:
        body = json.loads(raw["body"])
    except (ValueError, KeyError):
        return {"ok": False, "error": f"unparseable response (status {raw.get('status')})"}
    if raw["status"] >= 400:
        msg = (body.get("error") or {}).get("message") or f"HTTP {raw['status']}"
        return {"ok": False, "error": msg}
    cands = body.get("candidates") or []
    content = (cands[0].get("content") if cands else None) or {}
    parts = content.get("parts") or []
    if not parts:
        return {"ok": False, "error": "empty response from Gemini"}
    answer = "".join(p.get("text", "") for p in parts)
    sources, seen = [], set()
    gm = cands[0].get("groundingMetadata") or {}
    for chunk in gm.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        key = web.get("domain") or web.get("title") or ""
        if web.get("uri") and key not in seen:
            seen.add(key)
            sources.append({"url": web["uri"],
                            "title": web.get("title") or key,
                            "domain": key})
    return {"ok": True, "answer": answer, "sources": sources,
            "queries": gm.get("webSearchQueries") or [],
            "tokens": (body.get("usageMetadata") or {}).get("totalTokenCount", 0)}


def _resolve_redirects(sources):
    """Rewrite vertex grounding-redirect urls to their destinations via
    no-follow GETs reading `location` (ADR-008 §2). Best-effort."""
    uniq = sorted({s["url"] for s in sources})
    if not uniq:
        return
    results = effect("batch", {  # noqa: F821
        "name": "http.get",
        "payloads": [{"url": u, "timeout": 20, "redirects": 0} for u in uniq]})["results"]
    final = {}
    for u, r in zip(uniq, results, strict=False):
        if isinstance(r, dict) and (r.get("headers") or {}).get("location"):
            final[u] = r["headers"]["location"]
    for s in sources:
        s["url"] = final.get(s["url"], s["url"])
        s["domain"] = _domain(s["url"])


def _decompose(llm, question, answer):
    """3-7 follow-up questions + a collection name, or None when the
    classify reply doesn't parse (→ single-page fallback)."""
    prompt = (
        "You are analyzing the results of an initial web research.\n\n"
        f"Original question: {question}\n\n"
        f"Initial answer (summary):\n{answer[:2000]}\n\n"
        "Tasks:\n"
        "1. Generate 3-7 follow-up questions that would deepen this "
        "research. Use fewer (3-4) for narrow topics, more (5-7) for broad "
        "ones. Each should explore a specific aspect, fill a gap, or go "
        "deeper into something the initial answer only touched on. Make "
        "them concrete and searchable.\n"
        "2. Generate a short, descriptive collection name (3-6 words) for "
        "this research.\n\n"
        "Respond in JSON only:\n"
        '{"followUps": ["question1", "question2", ...], '
        '"collectionName": "Short Name"}'
    )
    reply = llm.chat([{"role": "user", "parts": [{"type": "text", "text": prompt}]}],
                     tier="classify", tools=[])
    text = " ".join(p["text"] for p in reply["parts"] if p["type"] == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    ups = parsed.get("followUps")
    if not isinstance(ups, list) or not ups:
        return None
    return {"followUps": [str(u) for u in ups[:_MAX_FOLLOW_UPS]],
            "collectionName": parsed.get("collectionName")
            or "Research: " + question[:40]}


def _page_type(c, space):
    """The built-in `page`: every document carries it (ADR-027 §3)."""
    return "page"


def _create_page(c, space, type_key, name, markdown):
    return c.create_object(space, {
        "types": [type_key], "name": name, "markdown": markdown})["objectId"]


def _sources_md(sources):
    return "\n".join(f"- [{s['title'] or s['domain']}]({s['url']})"
                     for s in sources)


@span(kind="mutator")  # noqa: F821 - guest global
def research(space, question, opts=None):
    """Research `question`; write the result pages into `space`.

    `opts`: `{"chatId": …}` posts progress bubbles to that chat while
    running (with `"agentName"`, default "bao"); omit for a silent
    run. Returns `{ok: True, overviewPageId, overviewPageName,
    subPages: [{id, name}], answer, sources: [{url, title, domain}],
    searchQueries, timing, usage}`; provider/config failures return
    `{ok: False, error}` instead of raising."""
    opts = opts or {}
    if not isinstance(question, str) or not question.strip():
        return {"ok": False, "error": "question is required"}
    c = use("any@v1")  # noqa: F821 - guest global
    llm = use("llm@v1")  # noqa: F821
    chat_id = opts.get("chatId")

    def bubble(text):
        if chat_id:
            c.chat_send(space, chat_id, {
                "text": text,
                "agent": {"name": opts.get("agentName", "bao"), "done": False}})

    t0 = now()  # noqa: F821 - guest global
    prov = _provider()
    timing = {}

    # phase 1 — initial grounded call
    bubble(f"Researching: {question[:100]}...")
    initial = _parse(effect("http.post", _request(prov, question)))  # noqa: F821
    timing["phase1Ms"] = int((now() - t0) * 1000)  # noqa: F821
    if not initial["ok"]:
        return {"ok": False, "error": "Initial search failed: " + initial["error"]}
    tokens = initial["tokens"]

    # phase 2 — follow-up decomposition
    bubble("Initial research done, clarifying follow-ups...")
    plan = _decompose(llm, question, initial["answer"])
    timing["phase2Ms"] = int((now() - t0) * 1000) - timing["phase1Ms"]  # noqa: F821

    all_sources = list(initial["sources"])
    followed = []
    if plan:
        # phase 3 — grounded calls on every follow-up, one crossing
        bubble(f"Researching {len(plan['followUps'])} follow-up topics...")
        raws = effect("batch", {  # noqa: F821
            "name": "http.post",
            "payloads": [_request(prov, q) for q in plan["followUps"]]})["results"]
        for q, p in zip(plan["followUps"], [_parse(r) for r in raws], strict=True):
            if p["ok"]:
                followed.append({"question": q, "answer": p["answer"],
                                 "sources": p["sources"]})
                all_sources.extend(p["sources"])
                tokens += p["tokens"]
    timing["phase3Ms"] = (int((now() - t0) * 1000)  # noqa: F821
                          - timing["phase1Ms"] - timing["phase2Ms"])

    # phase 4 — pages
    bubble("Creating research pages...")
    deduped, seen = [], set()
    for s in all_sources:
        key = s["domain"] or s["url"]
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    _resolve_redirects(deduped)
    type_key = _page_type(c, space)

    sub_pages = []
    for f in followed:
        md = f["answer"]
        if f["sources"]:
            md += "\n\n---\n\n## Sources\n\n" + _sources_md(f["sources"])
        sub_pages.append({"id": _create_page(c, space, type_key, f["question"], md),
                          "name": f["question"]})

    name = plan["collectionName"] if plan else "Research: " + question[:80]
    total_s = (now() - t0)  # noqa: F821
    overview = initial["answer"]
    if sub_pages:
        overview += "\n\n---\n\n## Follow-up Topics\n\n" + "\n".join(
            f"- [{p['name']}](any://o/{space}/{p['id']})" for p in sub_pages)
    if deduped:
        overview += "\n\n## Sources\n\n" + _sources_md(deduped)
    overview += (f"\n\n---\n\n*Gemini grounded search ({prov['model']}) | "
                 f"Total: {total_s:.1f}s*")
    overview_id = _create_page(c, space, type_key, name, overview)
    timing["totalMs"] = int(total_s * 1000)

    return {"ok": True, "overviewPageId": overview_id, "overviewPageName": name,
            "subPages": sub_pages, "answer": initial["answer"],
            "sources": deduped, "searchQueries": initial["queries"],
            "timing": timing, "usage": {"totalTokens": tokens}}


def main(args):
    if args and args.get("question") and args.get("space"):
        return research(args["space"], args["question"], args)
    return {"ok": False, "error": "space and question args required"}
