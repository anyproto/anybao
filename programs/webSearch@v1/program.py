"""Grounded web search — synthesized answers with real source urls.

Pass several queries at once (one round-trip): `ws.search("a", "b")`.
A failed query yields an `[ERROR] …` string in its slot; the others
still return. Use for anything needing current facts — prices,
versions, dates, news. For a multi-page investigation written into
the space, use `deepResearch@v1` instead."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Gemini generateContent + the google_search grounding tool (ADR-008
# §3); multi-query fan-out rides the batch effect (one guest→host
# crossing). Provider/model from config `search.provider.websearch`;
# the api key never enters the guest — the request names a credential
# ref and the host injects the header (ADR-002).

import json

_SYSTEM = (
    "You are a web-search backend. Given a query, search the web and "
    "answer in 4-8 sentences. Be concrete: cite numbers, dates, names, "
    "versions. Do not add preamble or hedging — just the answer."
)
_TIMEOUT_S = 120


def _provider():
    return effect("config.get", {"key": "search.provider.websearch"})["value"]  # noqa: F821


def _request(prov, query):
    url = (prov["base_url"].rstrip("/")
           + "/v1beta/models/" + prov["model"] + ":generateContent")
    return {
        "url": url,
        "json": {
            "system_instruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        },
        "timeout": _TIMEOUT_S,
        "credential": {"ref": prov["api_key_ref"], "header": "x-goog-api-key"},
    }


def _parse(raw):
    """One batch item -> {ok, answer, sources, queries} | {ok: False, error}.
    Sources dedup by url; grounding-redirect urls resolve later, in one
    pass across all queries."""
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
        uri = web.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append({"url": uri,
                            "title": web.get("title") or web.get("domain") or ""})
    return {"ok": True, "answer": answer, "sources": sources,
            "queries": gm.get("webSearchQueries") or []}


def _resolve_redirects(parsed):
    """Grounding sources arrive as vertexaisearch redirect urls; one
    recorded no-follow GET per unique url (redirects: 0 — ADR-008 §2)
    reads the location header, so the destination server is never
    touched. Best-effort: a failed resolve keeps the original url."""
    uniq = []
    seen = set()
    for p in parsed:
        for s in (p.get("sources") or []) if p["ok"] else []:
            if s["url"] not in seen:
                seen.add(s["url"])
                uniq.append(s["url"])
    if not uniq:
        return
    results = effect("batch", {  # noqa: F821
        "name": "http.get",
        "payloads": [{"url": u, "timeout": 20, "redirects": 0} for u in uniq]})["results"]
    final = {}
    for u, r in zip(uniq, results, strict=False):
        if isinstance(r, dict) and (r.get("headers") or {}).get("location"):
            final[u] = r["headers"]["location"]
    for p in parsed:
        for s in (p.get("sources") or []) if p["ok"] else []:
            s["url"] = final.get(s["url"], s["url"])


def _format(idx, query, p):
    if not p["ok"]:
        return f'[ERROR] query {idx} ("{query}") failed: {p["error"]}'
    primary = p["sources"][0]["url"] if p["sources"] else ""
    lines = [f"[{idx}] {query}", primary, "", p["answer"]]
    if len(p["sources"]) > 1:
        lines += ["", "Sources:"]
        lines += [f"- {s['title'] or s['url']} — {s['url']}"
                  for s in p["sources"][1:]]
    return "\n".join(lines)


@span("webSearch.search", kind="getter")  # noqa: F821 - guest global
def search(*queries):
    """Run one or more web searches; one formatted string per query.

    Variadic (a single list argument is also accepted). Each result,
    in query order: `[N] <query>` + the primary source url + a 4-8
    sentence synthesized answer + a `Sources:` list of the remaining
    grounding sources. A failed query yields `[ERROR] query N ("…")
    failed: <reason>` in its slot — the call itself never raises for
    a provider-side failure. Empty input returns `[]`."""
    if len(queries) == 1 and isinstance(queries[0], (list, tuple)):
        queries = tuple(queries[0])  # leniency: search([q1, q2]) == search(q1, q2)
    queries = [q if isinstance(q, str) else (q or {}).get("query", "")
               for q in queries]
    queries = [q for q in queries if q]
    if not queries:
        return []
    prov = _provider()
    raws = effect("batch", {  # noqa: F821
        "name": "http.post",
        "payloads": [_request(prov, q) for q in queries]})["results"]
    parsed = [_parse(r) for r in raws]
    _resolve_redirects(parsed)
    return [_format(i + 1, q, p)
            for i, (q, p) in enumerate(zip(queries, parsed, strict=True))]


def main(args):
    if args and args.get("query"):
        return search(args["query"])
    return search(*(args.get("queries") or [])) if args else []
