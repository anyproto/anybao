"""The http syscall — the ONE outbound door (ADR-002).

Everything the agent does to the world is an http call from guest
modules; the host contributes exactly what guest code must not hold:
route-derived read/mutate + capability truth (anybao.routes) and
NAMED-CREDENTIAL INJECTION — a payload carries
`credential: {"ref": <config key>, "header": <name>, "prefix"?: <str>}`
and the host resolves the secret and sets the header AFTER the payload
is recorded. The value never enters guest memory or the trace.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request

from anyrt.effects import Registry, effect

from .routes import Classifier

_DEFAULT_TIMEOUT = 180.0  # a stalled connection must ERROR, never hang


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


def register_http_effects(registry: Registry, *, secrets=None,
                          classifier: Classifier | None = None,
                          request=_http_request) -> None:
    """http.get/post/put/delete. `secrets(ref) -> value` resolves
    credential refs (config-backed in prod); `request` is injectable for
    offline tests. Authorization-style headers never appear in payloads,
    so there is nothing to redact — refs are trace-safe by construction."""
    cls = classifier or Classifier()

    def _inject(headers, credential) -> dict:
        if not credential:
            return headers or {}
        if secrets is None:
            raise RuntimeError("credential passed but no secrets resolver wired")
        out = dict(headers or {})
        prefix = credential.get("prefix", "")
        out[credential["header"]] = prefix + str(secrets(credential["ref"]))
        return out

    def _register(verb: str):
        method = verb.upper()

        @effect(f"http.{verb}", kind=cls.kind(method), registry=registry,
                cap=cls.cap(method))
        def http_verb(ctx, url, params=None, headers=None, json=None,
                      body=None, timeout=None, credential=None):
            return request(method, url, params=params,
                           headers=_inject(headers, credential),
                           json_body=json, body=body, timeout=timeout)

        return http_verb

    for verb in ("get", "post", "put", "delete"):
        _register(verb)
