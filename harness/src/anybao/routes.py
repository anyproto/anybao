"""Route classification — boundary-owned read/mutate + capability truth
for the http syscall (ADR-002 §2).

The host knows nothing about what guest modules DO with http; it knows
what a request IS: a read or a mutation, against which authority. That
is a property of (method, url) — data, not code. Any-API read-POSTs
(query/search/aggregate) are enumerated here; the api-drift manifest is
the upstream source when routes change.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# POST paths on the any API that are semantically reads.
_ANY_READ_POST_SUFFIXES = (
    "/query", "/objects/query", "/search", "/aggregate",
)

# LLM provider completion endpoints (reads: replayable model calls).
_LLM_PATH_SUFFIXES = ("/v1/messages", "/chat/completions", "/v1/complete")


class Classifier:
    """classify(payload) → kind / cap for one http call. `any_base`
    scopes data.* caps to the real any server; without it a path
    heuristic applies (tests, single-server dev)."""

    def __init__(self, any_base: str | None = None):
        self._any_netloc = urlsplit(any_base).netloc if any_base else None

    def _is_any(self, url: str) -> bool:
        parts = urlsplit(url)
        if self._any_netloc is not None:
            return parts.netloc == self._any_netloc
        return parts.path.startswith("/v1/spaces")  # heuristic fallback

    def _is_llm(self, url: str) -> bool:
        return urlsplit(url).path.endswith(_LLM_PATH_SUFFIXES)

    def kind(self, method: str):
        def _kind(payload: dict) -> str:
            url = payload.get("url", "")
            if method == "GET":
                return "read"
            if self._is_llm(url):
                return "read"  # model calls replay/mock like any read
            if method == "POST" and self._is_any(url) \
                    and urlsplit(url).path.endswith(_ANY_READ_POST_SUFFIXES):
                return "read"
            return "mutate"

        return _kind

    def cap(self, method: str):
        def _cap(payload: dict) -> str:
            url = payload.get("url", "")
            if self._is_llm(url):
                return "llm.chat"
            if self._is_any(url):
                return "data.read" if self.kind(method)(payload) == "read" \
                    else "data.write"
            return "net.http"

        return _cap
