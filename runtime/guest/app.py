"""Guest kernel — runs INSIDE CPython-on-WASI (componentize-py world
`kernel`). ADR-002 §3/§4: deny-by-default namespace, curated builtins,
import allowlist + proxied ambient-authority modules, Pythonic facades.
Everything nondeterministic routes through `host-effect` (ADR-003).
"""

import ast

# Literal imports so componentize-py BUNDLES these into the guest (its
# static analysis can't see lazy imports) — every tier-1 allowlist
# module plus datetime (proxied). Do not convert to importlib loops.
import base64  # noqa: F401
import bisect  # noqa: F401
import builtins as _b
import collections  # noqa: F401
import contextlib  # noqa: F401
import copy  # noqa: F401
import dataclasses  # noqa: F401
import datetime  # noqa: F401  (guest sees only the proxy)
import decimal  # noqa: F401
import email.header  # noqa: F401  (tier-1, ADR-012 §6; email is lazy —
import email.utils  # noqa: F401   literal submodule imports bundle them)
import enum  # noqa: F401
import fractions  # noqa: F401
import functools  # noqa: F401
import hashlib  # noqa: F401
import heapq  # noqa: F401
import html.entities  # noqa: F401  (tier-1, ADR-012 §6; also bs4's backend)
import html.parser  # noqa: F401
import inspect  # noqa: F401
import itertools  # noqa: F401
import json
import math  # noqa: F401
import re  # noqa: F401
import statistics  # noqa: F401
import string  # noqa: F401
import textwrap  # noqa: F401
import traceback
import types
import typing  # noqa: F401
import unicodedata  # noqa: F401

import bs4  # noqa: F401  (vendored, ADR-012 §6 — with soupsieve/typing_extensions)
import markdownify  # noqa: F401  (vendored, ADR-012 §6 — with six)
import wit_world


class EffectError(Exception):
    pass


def _effect(name, payload=None):
    reply = json.loads(wit_world.host_effect(name, json.dumps(payload or {})))
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise EffectError(f"{err.get('type', 'EffectError')}: {err.get('message', '')}")
    return reply.get("output")


# ---- shim globals (ADR-002 resolved Q1/Q2) --------------------------------

def now():
    """Wall-clock via the time.now effect (recorded)."""
    return _effect("time.now")["epoch"]


def rand():
    return _effect("random.random")["value"]


def env(name, default=None):
    out = _effect("env.get", {"name": name})
    return out["value"] if out["present"] else default


def uuid4():
    return _effect("uuid4")["hex"]


# ---- http facade (ADR-002 §1 Pythonic surface) -----------------------------

class Response:
    def __init__(self, raw):
        self.status = raw.get("status")
        self.headers = raw.get("headers") or {}
        self.text = raw.get("body") or ""
        self.url = raw.get("url")  # final url, post-redirect (ADR-008 §2)

    def json(self):
        return json.loads(self.text)

    def __repr__(self):
        return f"<Response {self.status}, {len(self.text)} bytes>"


def _batch(name, payloads):
    out = _effect("batch", {"name": name, "payloads": payloads})["results"]
    resolved = []
    for r in out:
        if isinstance(r, dict) and set(r) == {"error"}:  # host batch item failure
            resolved.append(EffectError(f"{r['error']['type']}: {r['error']['message']}"))
        else:
            resolved.append(r)
    return resolved


class _Http:
    def _call(self, verb, url, **kw):
        return Response(_effect(f"http.{verb}", {"url": url, **kw}))

    def get_many(self, items, **common):
        """items: list of urls or {url, ...} dicts. One crossing, host
        concurrency, results in input order (ADR-002 resolved Q3)."""
        payloads = [
            {"url": it, **common} if isinstance(it, str) else {**common, **it}
            for it in items
        ]
        return [r if isinstance(r, EffectError) else Response(r)
                for r in _batch("http.get", payloads)]

    def get(self, url, **kw):
        return self._call("get", url, **kw)

    def head(self, url, **kw):
        return self._call("head", url, **kw)

    def post(self, url, **kw):
        return self._call("post", url, **kw)

    def put(self, url, **kw):
        return self._call("put", url, **kw)

    def patch(self, url, **kw):
        return self._call("patch", url, **kw)

    def delete(self, url, **kw):
        return self._call("delete", url, **kw)


http = _Http()

# ---- proxied stdlib (tier 2: ambient authority -> effects) -----------------

_proxy_cache: dict = {}


def _datetime_proxy():
    import datetime as _dt

    class _DateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(_effect("time.now")["epoch"], tz)

        @classmethod
        def today(cls):
            return cls.now()

    class _Date(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date.fromtimestamp(_effect("time.now")["epoch"])

    return types.SimpleNamespace(
        datetime=_DateTime, date=_Date, time=_dt.time,
        timedelta=_dt.timedelta, timezone=_dt.timezone, UTC=_dt.UTC,
    )


def _random_proxy():
    def _sample(seq, k):
        pool = list(seq)
        return [pool.pop(int(rand() * len(pool))) for _ in range(k)]

    return types.SimpleNamespace(
        random=rand,
        uniform=lambda a, b: a + rand() * (b - a),
        randint=lambda a, b: a + int(rand() * (b - a + 1)),
        choice=lambda seq: seq[int(rand() * len(seq))],
        sample=_sample,
        shuffle=lambda lst: lst.sort(key=lambda _: rand()),
    )


def _time_proxy():
    return types.SimpleNamespace(
        time=now,
        monotonic=now,  # good enough for cell code; real monotonic is ambient
        sleep=lambda s: _effect("sleep", {"seconds": s}),
    )


class _Environ:
    def get(self, name, default=None):
        return env(name, default)

    def __getitem__(self, name):
        out = _effect("env.get", {"name": name})
        if not out["present"]:
            raise KeyError(name)
        return out["value"]

    def __contains__(self, name):
        return _effect("env.get", {"name": name})["present"]


def _os_proxy():
    return types.SimpleNamespace(environ=_Environ())


_PROXIES = {
    "datetime": _datetime_proxy,
    "random": _random_proxy,
    "time": _time_proxy,
    "os": _os_proxy,
}

# tier 1: pure stdlib, passes through (ADR-002 §4; inspect: ADR-010 §2;
# ast: ADR-013 §3 — the program write path's syntax gate/source scanner;
# html/email + the vendored trio: ADR-012 §6). six/typing_extensions are
# bundled as internals of the vendored packages but stay un-importable.
_ALLOWED = {
    "math", "json", "re", "itertools", "functools", "collections",
    "contextlib", "textwrap", "heapq", "bisect", "statistics",
    "dataclasses", "enum", "typing", "decimal", "fractions", "base64",
    "hashlib", "string", "copy", "unicodedata", "inspect", "ast",
    "html", "email",
    "bs4", "soupsieve", "markdownify",   # vendored pure-Python (runtime/guest/)
}


def _guest_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if top in _PROXIES:
        if top not in _proxy_cache:
            _proxy_cache[top] = _PROXIES[top]()
        return _proxy_cache[top]
    if top in _ALLOWED:
        return _b.__import__(name, globals, locals, fromlist, level)
    raise ImportError(
        f"module '{name}' is outside the effect boundary. "
        f"Available: {', '.join(sorted(_ALLOWED))}; "
        f"proxied: {', '.join(sorted(_PROXIES))}; "
        f"plus globals http, now(), rand(), env(), uuid4(), values, effects, "
        f"effect(), span(), describe(), inferSchema(), help()."
    )


# ---- native introspection (ADR-010 §2) --------------------------------------

def _sig_of(fn):
    try:
        return str(inspect.signature(fn))
    except (ValueError, TypeError):
        return "(…)"


def _first_doc_line(obj):
    for ln in (inspect.getdoc(obj) or "").splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _fn_heading(name, fn):
    kind = getattr(fn, "__span_kind__", None)
    return f"{name}{_sig_of(fn)}" + (f" [{kind}]" if kind else "")


def describe(obj):
    """Render an object's API from its code (ADR-010 §2): function →
    `name(sig) [kind]` + full docstring; module/class/instance →
    docstring, then one `name(sig) [kind] — summary` line per public
    method (`_`-names and `main` hidden, ADR-010 §1). ONE renderer:
    help() prints this and the toolcaller's `## Tools` embeds it."""
    if inspect.isroutine(obj):
        doc = inspect.getdoc(obj) or ""
        return _fn_heading(obj.__name__, obj) + ("\n" + doc if doc else "")
    if inspect.ismodule(obj):
        # functions DEFINED here (kernel-injected globals are not)
        items = [(n, f) for n, f in vars(obj).items()
                 if isinstance(f, types.FunctionType)
                 and f.__module__ == obj.__name__]
    else:
        cls = obj if isinstance(obj, type) else type(obj)
        items = [(n, getattr(obj, n)) for n, f in vars(cls).items()
                 if isinstance(f, types.FunctionType)]
    lines = []
    for n, f in items:
        if n.startswith("_") or n == "main":
            continue
        summary = _first_doc_line(f)
        lines.append("  " + _fn_heading(n, f)
                     + (f" — {summary}" if summary else ""))
    parts = [p for p in [inspect.getdoc(obj) or ""] if p]
    if lines:
        parts.append("Methods:\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else f"<{type(obj).__name__}>"


def _help(obj):
    """Curated help: print(describe(obj)) through the cell's traced
    print — the structured output channel, never pydoc's pager."""
    text = describe(obj)
    p = _ns.get("print")
    if callable(p):
        p(text)
        return None
    return text  # no cell printer (module top level): hand back the text


# ---- curated builtins (ADR-002 §3) -----------------------------------------

_SAFE_NAMES = [
    "len", "range", "enumerate", "zip", "sorted", "reversed", "min", "max",
    "sum", "abs", "round", "divmod", "pow", "dict", "list", "set",
    "frozenset", "tuple", "str", "int", "float", "bool", "bytes",
    "bytearray", "complex", "isinstance", "issubclass", "repr", "format",
    "hash", "type", "getattr", "setattr", "hasattr", "iter", "next",
    "slice", "map", "filter", "any", "all", "chr", "ord", "hex", "oct",
    "bin", "dir", "callable", "object", "super", "staticmethod",
    "classmethod", "property", "memoryview", "None", "True", "False",
    "NotImplemented", "Ellipsis", "__build_class__", "__name__",
]
# excluded on purpose: open, input, eval, exec, compile, breakpoint,
# globals, locals, vars, raw __import__ (replaced by _guest_import),
# pydoc's help (replaced by the curated _help, ADR-010 §2)


def _safe_builtins() -> dict:
    out = {}
    for n in _SAFE_NAMES:
        if hasattr(_b, n):
            out[n] = getattr(_b, n)
    for n in dir(_b):  # every exception/warning class
        obj = getattr(_b, n)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            out[n] = obj
    out["__import__"] = _guest_import
    out["help"] = _help
    return out


_SAFE_BUILTINS = _safe_builtins()

# ---- value store (ADR-003 §4, guest side; real objects) --------------------

_values: dict = {}  # cell_id -> {"prints": [obj, ...], "last": obj}


class _Values:
    def get(self, cell_id, i="last"):
        entry = _values.get(cell_id)
        if entry is None:
            raise KeyError(f"no values for cell {cell_id!r}")
        if i == "last":
            if "last" not in entry:
                raise KeyError(f"cell {cell_id!r} has no last value")
            return entry["last"]
        return entry["prints"][i]

    def list(self):
        return {
            cid: {"n_prints": len(e["prints"]), "has_last": "last" in e}
            for cid, e in _values.items()
        }


values = _Values()


class _Effects:
    """Trace views (ADR-003 §4) — the effects half of the kernel API."""

    def of(self, cell_id=None, *, span=None):
        """A scope's IMMEDIATE children (ADR-001 §4d): pass a `cell_id`
        for the cell's top level, or `span=<id>` to expand one facade
        span into its inner effects + child span rows. Each span row
        carries its own `span` id — recurse to drill deeper."""
        q = {}
        if cell_id is not None:
            q["cell"] = cell_id
        if span is not None:
            q["span"] = span
        return _effect("trace.effects_of", q)["records"]

    def get(self, seq):
        """One full record by seq — an effect or a span (ADR-003 §4)."""
        return _effect("trace.effect_get", {"seq": seq})


effects = _Effects()

# ---- span: composite facades lift to one record pair (ADR-003 §4b) ---------

def _json_safe(v):
    """Span input/output must be trace-serializable — non-JSON guest
    values degrade to repr (ADR-001 §4c)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return repr(v)


def _span_input(argnames, drop_self, args, kwargs):
    """Facade input keyed by PARAMETER NAME (ADR-003 §4b): a leading
    `self` is dropped, extra positionals fall back to `argN`, kwargs
    merge in. Trace-serializable via _json_safe — never the bound
    instance, never an opaque positional `args` list."""
    names = argnames[1:] if drop_self else argnames
    posargs = args[1:] if drop_self else args
    inp = {}
    for i, v in enumerate(posargs):
        inp[names[i] if i < len(names) else f"arg{i}"] = _json_safe(v)
    for k, v in kwargs.items():
        inp[k] = _json_safe(v)
    return inp


def span(name=None, kind=None):
    """Decorator: group one facade call's effects under a single trace
    input/output pair, so views show `name(args) -> out` like a host
    effect (ADR-001 §4c); the inner effect records stay underneath
    (expand to see). This is the guest-side effect wrapper for tool
    methods (ADR-003 §4b).

    `name` defaults to `<module>.<function>` read off the decorated def
    (ADR-003 §4b, 2026-08-14) — the anchor cannot drift from the code.
    Pass it only as a deliberate display override (e.g. a hidden `_def`
    spanning under a public trace name). `kind`
    (getter|mutator|setup|program) is the guest-DECLARED narrative
    classification recorded on the span (ADR-001 §4d) — it is NOT the
    mutation oracle: `meta.mutations` (count of inner mutate effects,
    boundary-owned) is. A raised exception ends the span ok:false with a
    clean {type, message} (no traceback) and re-raises."""
    def deco(fn):
        span_name = name
        if span_name is None:
            mod = fn.__globals__.get("__name__") or ""
            span_name = f"{mod}.{fn.__name__}" if mod else fn.__name__
        argnames = fn.__code__.co_varnames[:fn.__code__.co_argcount]
        drop_self = bool(argnames) and argnames[0] == "self"

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            payload = {"name": span_name,
                       "input": _span_input(argnames, drop_self, args, kwargs)}
            if kind is not None:
                payload["kind"] = kind
            _effect("span.begin", payload)
            try:
                out = fn(*args, **kwargs)
            except BaseException as e:
                _effect("span.end", {"ok": False, "error": {
                    "type": type(e).__name__, "message": str(e)}})
                raise
            _effect("span.end", {"ok": True, "output": _json_safe(out)})
            return out
        # readable off the function (ADR-010 §1) — describe()/help()
        # render it as the `[kind]` tag; signature/doc survive wraps()
        wrapped.__span_kind__ = kind
        return wrapped
    return deco


# ---- use() module loading (ADR-004) ----------------------------------------

_module_cache: dict = {}   # (objectId, marker) -> module object


def _bound_use(owner):
    """A use() carrying its OWNER's identity (ADR-004 §2.4/§5): every
    loaded module gets its own binding, so its use() calls resolve in
    its defining space no matter WHEN they run — top-level or from a
    function called long after load (a push/pop frame stack only covers
    top-level and broke exactly there). Cell code gets the unowned
    binding (owner None → the working space)."""
    def use(spec):
        """Load a space program by `name@vN` spec (ADR-004 §1). Probe
        every call (host cache validates by marker)."""
        r = _effect("module.resolve", {"spec": spec, "frm": owner})
        ck = (r["objectId"], r["marker"])
        if ck in _module_cache:
            return _module_cache[ck]
        mod = types.ModuleType(spec.split("@")[0].split(":")[-1])
        mod.__dict__.update(_fresh_ns())
        mod.__dict__["use"] = _bound_use(r["objectId"])
        exec(compile(r["source"], f"<{spec}>", "exec"), mod.__dict__)
        _module_cache[ck] = mod
        return mod
    return use


use = _bound_use(None)

# ---- value metadata for the digest (ADR-003 ValueRef / ADR-005 §4) ---------

def _schema(v, depth=0):
    """Compact structural descriptor of a VALUE — the data-shape
    counterpart of describe() (ADR-010 §2): dict keys with nested
    shapes, list length × element shape, scalars by type. The host
    digest shows it in value stubs; cells call it as `inferSchema(v)`
    to ground filters/writes in an observed shape instead of a
    guessed one."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return type(v).__name__
    if isinstance(v, (list, tuple)):
        inner = _schema(v[0], depth + 1) if v and depth < 2 else "…"
        return f"{type(v).__name__}[{len(v)} × {inner}]"
    if isinstance(v, dict):
        if depth < 2:
            keys = list(v)[:8]
            shape = ", ".join(f"{k}:{_schema(v[k], depth + 1)}" for k in keys)
            more = ", …" if len(v) > 8 else ""
            return "{" + shape + more + "}"
        return f"dict[{len(v)}]"
    return type(v).__name__


def _value_meta(v, display=repr):
    # prints render str-like (no quotes on strings, like Python print);
    # last-expression values render repr-like (REPL semantics).
    r = display(v)
    return {"repr": r, "size": len(r), "schema": _schema(v)}


# ---- namespace & cell execution --------------------------------------------

_ns: dict = {}


def _fresh_ns() -> dict:
    return {
        "__builtins__": _SAFE_BUILTINS,
        "effect": _effect,           # raw channel (plumbing; facades preferred)
        "EffectError": EffectError,
        "http": http,
        "now": now,
        "rand": rand,
        "env": env,
        "uuid4": uuid4,
        "values": values,
        "effects": effects,
        "span": span,
        "use": use,
        "describe": describe,   # the doc renderer (ADR-010 §2)
        "inferSchema": _schema,  # the data-shape renderer (ADR-010 §2)
        "subcell": _run_cell,   # cell-in-cell: the toolcaller's executor
    }


def _run_cell(code: str, cell_id: str) -> dict:
    """Cell semantics (ADR-003 §2) — module-level so guest programs can
    drive cells too (the toolcaller's `subcell`); the wit export wraps
    this in JSON for the host protocol."""
    global _ns
    if not _ns:
        _ns = _fresh_ns()
    store = _values.setdefault(cell_id, {"prints": []})
    prints: list[dict] = []

    def _print(*a, **kw):
        # single arg: keep the structured value (schema/size for the
        # digest). multi arg: Python's space-join, a formatted line.
        v = a[0] if len(a) == 1 else " ".join(
            x if isinstance(x, str) else repr(x) for x in a
        )
        store["prints"].append(v)
        prints.append(_value_meta(v, display=str))

    prev_print = _ns.get("print")   # nested cells restore the caller's printer
    _ns["print"] = _print
    try:
        tree = ast.parse(code, mode="exec")
        last = None
        has_last = False
        tail = tree.body[-1] if tree.body else None
        if isinstance(tail, ast.Expr):
            tree.body.pop()
            last_expr = ast.Expression(tail.value)
            exec(compile(tree, "<cell>", "exec"), _ns)
            last = eval(compile(last_expr, "<cell>", "eval"), _ns)
            has_last = last is not None
        else:
            exec(compile(tree, "<cell>", "exec"), _ns)
        if has_last:
            store["last"] = last
        return {
            "ok": True,
            "prints": prints,
            "last": _value_meta(last) if has_last else None,
            "error": None,
        }
    except BaseException as e:  # incl. MemoryError; traps never reach here
        return {
            "ok": False,
            "prints": prints,
            "last": None,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(limit=8),
            },
        }
    finally:
        if prev_print is not None:
            _ns["print"] = prev_print


class WitWorld:
    def run_cell(self, code: str, cell_id: str) -> str:
        return json.dumps(_run_cell(code, cell_id))

    def reset_ns(self) -> None:
        global _ns
        _ns = {}
        _values.clear()
        _proxy_cache.clear()
        _module_cache.clear()
