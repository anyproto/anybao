"""Guest kernel — runs INSIDE CPython-on-WASI (componentize-py world
`kernel`). Persistent namespace across cells; print capture; last-
expression value; `effect(name, payload)` bridging to the host broker.

M0 scope: default builtins remain (the cage already denies the world —
no sockets/fs/env are linked); namespace curation + proxied
datetime/random land in M1 per ADR-002 §3/§4.
"""

import ast
import json
import traceback

import wit_world


class _EffectError(Exception):
    pass


def _effect(name, payload=None):
    reply = json.loads(wit_world.host_effect(name, json.dumps(payload or {})))
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise _EffectError(f"{err.get('type', 'EffectError')}: {err.get('message', '')}")
    return reply.get("output")


_ns: dict = {}


def _fresh_ns() -> dict:
    return {"effect": _effect, "EffectError": _EffectError}


class WitWorld:
    def run_cell(self, code: str) -> str:
        global _ns
        if not _ns:
            _ns = _fresh_ns()
        prints: list[str] = []
        _ns["print"] = lambda *a, **kw: prints.append(" ".join(repr(x) if not isinstance(x, str) else x for x in a))
        try:
            tree = ast.parse(code, mode="exec")
            last = None
            has_last = False
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr = ast.Expression(tree.body.pop().value)
                exec(compile(tree, "<cell>", "exec"), _ns)
                last = eval(compile(last_expr, "<cell>", "eval"), _ns)
                has_last = last is not None
            else:
                exec(compile(tree, "<cell>", "exec"), _ns)
            return json.dumps(
                {
                    "ok": True,
                    "prints": prints,
                    "last": repr(last) if has_last else None,
                    "error": None,
                }
            )
        except Exception as e:
            return json.dumps(
                {
                    "ok": False,
                    "prints": prints,
                    "last": None,
                    "error": {
                        "type": type(e).__name__,
                        "message": str(e),
                        "traceback": traceback.format_exc(limit=8),
                    },
                }
            )

    def reset_ns(self) -> None:
        global _ns
        _ns = {}
