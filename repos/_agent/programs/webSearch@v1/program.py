"""Grounded web search — synthesized answers with real source urls.

Pass several queries at once (one round-trip): `ws.search("a", "b")`.
A failed query yields an `[ERROR] …` string in its slot; the others
still return. Use for anything needing current facts — prices,
versions, dates, news. For a multi-page investigation written into
the space, use `deepResearch@v1` instead."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Two wires, picked by the provider row's `provider` (ADR-008 §3):
# `gemini` (default) = generateContent + the google_search grounding
# tool; `openai-compat` = chat completions + `web_search_options`,
# sources from the reply's `url_citation` annotations (OpenAI's search
# models, the any-ui local AI proxy). Multi-query fan-out rides the
# batch effect (one guest→host crossing). Provider/model from config
# `search.provider.websearch`; the api key never enters the guest —
# the request names a credential ref and the host injects the header
# (ADR-002).

import json

_SYSTEM = (
    "You are a web-search backend. Given a query, search the web and "
    "answer in 4-8 sentences. Be concrete: cite numbers, dates, names, "
    "versions. Do not add preamble or hedging — just the answer."
)
_TIMEOUT_S = 120
_MAX_SOURCES = 10  # an any-ai search cites every hit (up to 64)


def _provider():
    return effect("config.get", {"key": "search.provider.websearch"})["value"]  # noqa: F821


def _wire(prov):
    """The provider row's wire (ADR-008 §3) — a config error before any call."""
    wire = prov.get("provider") or "gemini"
    if wire not in _WIRES:
        raise ValueError(f"search.provider.websearch: unknown provider {wire!r}; "
                         f"supported: {', '.join(_WIRES)}")
    return _WIRES[wire]


def _gemini_request(prov, query):
    url = (prov["base_url"].rstrip("/")
           + "/v1beta/models/" + prov["model"] + ":generateContent")
    return {
        "url": url,
        "json": {
            "system_instruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            # a 4-8 sentence synthesis needs no deliberation, and
            # thinking tokens bill at the output rate (ADR-008 §3)
            "generation_config": {"thinking_config": {"thinking_level": "low"}},
        },
        "timeout": _TIMEOUT_S,
        # `about` (ADR-021 §1): what the Credentials card shows when the
        # key is missing — label, the only host it goes to, where to get it
        "credential": {"ref": prov["api_key_ref"], "header": "x-goog-api-key",
                       "about": {"label": "Gemini API key",
                                 "hosts": [_host(prov["base_url"])],
                                 "help": "https://aistudio.google.com/apikey"}},
    }


def _host(base_url):
    return base_url.split("//", 1)[-1].split("/", 1)[0]


def _openai_request(prov, query):
    req = {
        "url": prov["base_url"].rstrip("/") + "/chat/completions",
        "json": {
            "model": prov["model"],
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": query}],
            "web_search_options": {},
        },
        "timeout": _TIMEOUT_S,
    }
    # a self-hosted endpoint may need no key (`api_key_ref: null`); a
    # named one is a bearer token bound to the base_url host (ADR-021 §8.1)
    if prov.get("api_key_ref"):
        host = _host(prov["base_url"])
        req["credential"] = {"ref": prov["api_key_ref"], "header": "Authorization",
                             "prefix": "Bearer ",
                             "about": {"label": f"{host} API key", "hosts": [host]}}
    return req


# ADR-021 §8.1: the search tier's default key, declared with its host
# (a tier re-pointed at another provider names that provider's ref)
__any_credentials__ = [{"ref": "google.key.gemini",
                        "about": {"label": "Gemini API key",
                                  "hosts": ["generativelanguage.googleapis.com"],
                                  "help": "https://aistudio.google.com/apikey"}}]


def _parse(raw, wire):
    """One batch item -> {ok, answer, sources} | {ok: False, error}.
    Any body shape lands in its own slot — a 200 that is not the
    wire's object is an error there, never a raise out of search()."""
    if isinstance(raw, dict) and set(raw) == {"error"}:  # batch item failure
        return {"ok": False,
                "error": f"{raw['error']['type']}: {raw['error']['message']}"}
    try:
        body = json.loads(raw["body"])
    except (ValueError, KeyError, TypeError):  # TypeError: a raw-bytes blob ref
        return {"ok": False, "error": f"unparseable response (status {raw.get('status')})"}
    if not isinstance(body, dict):
        return {"ok": False, "error": f"unexpected response (status {raw['status']})"}
    if raw["status"] >= 400:
        err = body.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or f"HTTP {raw['status']}"
        return {"ok": False, "error": str(msg)}
    try:
        return wire["parse"](body)
    except (AttributeError, TypeError, IndexError, KeyError):
        return {"ok": False, "error": f"malformed response (status {raw['status']})"}


def _sources(pairs):
    """(url, title) pairs -> sources, first url wins, capped."""
    out = {}
    for url, title in pairs:
        if url and url not in out:
            out[url] = {"url": url, "title": title or ""}
    return list(out.values())[:_MAX_SOURCES]


def _gemini_parse(body):
    cands = body.get("candidates") or []
    content = (cands[0].get("content") if cands else None) or {}
    parts = content.get("parts") or []
    if not parts:
        return {"ok": False, "error": "empty response from Gemini"}
    answer = "".join(p.get("text", "") for p in parts)
    gm = cands[0].get("groundingMetadata") or {}
    webs = [c.get("web") or {} for c in gm.get("groundingChunks") or []]
    return {"ok": True, "answer": answer,
            "sources": _sources((w.get("uri"), w.get("title") or w.get("domain"))
                                for w in webs)}


def _openai_parse(body):
    choices = body.get("choices") or []
    msg = (choices[0].get("message") if choices else None) or {}
    answer = msg.get("content") or ""
    if isinstance(answer, list):  # content parts
        answer = "".join(p.get("text", "") for p in answer if p.get("type") == "text")
    if not answer:
        if msg.get("refusal"):
            return {"ok": False, "error": f"refused: {msg['refusal']}"}
        return {"ok": False, "error": "empty response from the search model"}
    cites = [a.get("url_citation") or {} for a in msg.get("annotations") or []
             if a.get("type") == "url_citation"]
    return {"ok": True, "answer": str(answer),
            "sources": _sources((c.get("url"), c.get("title")) for c in cites)}


# provider -> its wire; `redirects`: the sources are grounding redirects
_WIRES = {"gemini": {"request": _gemini_request, "parse": _gemini_parse, "redirects": True},
          "openai-compat": {"request": _openai_request, "parse": _openai_parse,
                            "redirects": False}}


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


@span(kind="getter")  # noqa: F821 - guest global
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
    wire = _wire(prov)
    raws = effect("batch", {  # noqa: F821
        "name": "http.post",
        "payloads": [wire["request"](prov, q) for q in queries]})["results"]
    parsed = [_parse(r, wire) for r in raws]
    # only Gemini's grounding urls are redirects; a GET of a real source
    # url would touch the destination server (ADR-008 §2)
    if wire["redirects"]:
        _resolve_redirects(parsed)
    return [_format(i + 1, q, p)
            for i, (q, p) in enumerate(zip(queries, parsed, strict=True))]


def main(args):
    if args and args.get("query"):
        return search(args["query"])
    return search(*(args.get("queries") or [])) if args else []
