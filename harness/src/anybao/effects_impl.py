"""Real effect implementations the runner registers on the broker —
the host side of the guest facades (ADR-002). `http.*` is the fetch
replacement (arbitrary outbound HTTP); `chat.send` posts to a chat
object via anyclient. Registered like any effect, so they trace and
replay. Credentials/keys are resolved HERE, inside the boundary
(M6 named-credential injection is a later hook).
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request

from anyrt.effects import Registry, effect

from .anyclient import AnyClient

_DEFAULT_TIMEOUT = 30.0


def _http_request(method: str, url: str, *, params=None, headers=None,
                  json_body=None, body=None, timeout=None) -> dict:
    if params:
        from urllib.parse import urlencode
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = _json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout or _DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
            return {"status": resp.status,
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": raw.decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return {"status": e.code,
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
                "body": raw.decode(errors="replace")}


def register_http_effects(registry: Registry) -> None:
    """http.get (read) / post|put|delete (mutate). Authorization header
    redacted from the trace."""
    redact = ("headers.authorization",)

    @effect("http.get", kind="read", registry=registry, redact=redact, cap="net.http")
    def http_get(ctx, url, params=None, headers=None, timeout=None):
        return _http_request("GET", url, params=params, headers=headers, timeout=timeout)

    @effect("http.post", kind="mutate", registry=registry, redact=redact, cap="net.http")
    def http_post(ctx, url, json=None, body=None, headers=None, timeout=None):
        return _http_request("POST", url, json_body=json, body=body,
                             headers=headers, timeout=timeout)

    @effect("http.put", kind="mutate", registry=registry, redact=redact, cap="net.http")
    def http_put(ctx, url, json=None, body=None, headers=None, timeout=None):
        return _http_request("PUT", url, json_body=json, body=body,
                             headers=headers, timeout=timeout)

    @effect("http.delete", kind="mutate", registry=registry, redact=redact, cap="net.http")
    def http_delete(ctx, url, headers=None, timeout=None):
        return _http_request("DELETE", url, headers=headers, timeout=timeout)


def register_chat_effect(registry: Registry, client: AnyClient, *, space: str, chat_id: str,
                         agent_name: str = "bao") -> None:
    """chat.send posts to THE conversation's chat object. `done` is the
    liveness flag (progress bubbles = done:false); every run ends
    done:true. `agent` marks it agent-authored (so the watcher's
    agent-skip filter ignores it — no self-trigger)."""

    @effect("chat.send", kind="mutate", registry=registry, cap="chat.write")
    def chat_send(ctx, text, done=True, debug_link=None):
        agent = {"name": agent_name, "done": done}
        if debug_link:
            agent["debugLink"] = debug_link
        return client.chat_send(space, chat_id, {"text": text, "agent": agent})
