"""Kernel-fidelity guest exec for tests: import the REAL guest kernel
(runtime/guest/app.py) host-side with `wit_world` stubbed, so program
tests run under the kernel's actual semantics — curated builtins, the
import allowlist (a stray `import contextlib` fails HERE, exactly as in
the wasm guest), span machinery, and use() module loading — instead of
plain host CPython. The only fake is the effect boundary itself.

`load_kernel(effect, any_client=..., llm_chat=...)` returns the app
module; `app.use("name@vN")` then loads real sources from programs/
(same flat-file / folder order as the runtime's local_source_path).
`any@v1` / `llm@v1` resolve to dispatch shims when a fake is given —
their calls cross the effect boundary as `test.any` / `test.llm`, so
existing fake-client objects keep working unchanged. Effects the
harness doesn't own (http.*, config.get, time.now, …) go to `effect`;
span.begin/end are absorbed. Unknown effects raise loudly.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS_DIR = ROOT / "repos" / "_agent" / "programs"
APP_PY = ROOT / "runtime" / "guest" / "app.py"

_n = 0

# use("any@v1") stand-in: every client method call crosses the effect
# boundary as test.any; the host dispatches to the fake client object.
ANY_SHIM = '''
class AnyError(Exception):
    def __init__(self, status=0, code="", message=""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


def __getattr__(name):   # flat module surface (ADR-010 §8)
    def _call(*args, **kwargs):
        out = effect("test.any", {"method": name, "args": list(args),
                                  "kwargs": kwargs})
        if isinstance(out, dict) and "__error__" in out:
            raise AnyError(out.get("__status__", 0),
                           out.get("__code__", "test"), out["__error__"])
        return out
    return _call
'''

# use("llm@v1") stand-in: chat crosses as test.llm.
LLM_SHIM = '''
class LlmError(Exception):
    pass


def chat(messages, system="", tier="codegen", tools=None, max_tokens=None):
    return effect("test.llm", {"messages": messages, "system": system,
                               "tier": tier, "tools": tools,
                               "max_tokens": max_tokens})
'''


def local_source(spec, programs_dir=None):
    """programs/<spec>.py or programs/<spec>/program.py — the same
    resolution order as the runtime's local_source_path."""
    root = programs_dir or PROGRAMS_DIR
    p = root / f"{spec}.py"
    if not p.exists():
        p = root / spec / "program.py"
    return p.read_text()


def load_kernel(effect=None, any_client=None, llm_chat=None, programs_dir=None,
                module_source=None, shell=None):
    """A fresh kernel app module wired to test fakes. `effect(name,
    payload) -> output` serves pass-through effects; `any_client` /
    `llm_chat`, when given, shadow the real any@v1 / llm@v1 modules.
    `programs_dir` overrides the source root (another repo's tests —
    e.g. repos/_connectors — resolve their own programs/).
    `module_source(spec)`, when given, is consulted first for other
    specs: return source text, `{"source", "marker"?}` (marker drives
    the guest probe cache — bump it to model an edited program,
    ADR-004 §4), or None to fall through to programs_dir.
    `shell`, when given, is what `runtime.get("shell")` returns (the
    binary has the feature → `sh`/`fs` bound in cells, ADR-024 §6);
    the default models a binary without it — the probe reads null
    and the names stay out of the namespace — unless `effect` chooses
    to serve the key itself."""
    def host_effect(name, payload_json):
        payload = json.loads(payload_json)
        try:
            if name == "module.resolve":
                spec = payload["spec"]
                marker = "m0"
                # alias-qualified cross-repo specs ("agent:any@v1",
                # ADR-009) shim identically — the alias names a space,
                # the module is the same
                if spec.split(":")[-1] == "any@v1" and any_client is not None:
                    src = ANY_SHIM
                elif spec.split(":")[-1] == "llm@v1" and llm_chat is not None:
                    src = LLM_SHIM
                else:
                    src = None
                    if module_source is not None:
                        hit = module_source(spec)
                        if isinstance(hit, dict):
                            src = hit.get("source")
                            marker = hit.get("marker", marker)
                        elif hit is not None:
                            src = hit
                    if src is None:
                        # the local dir serves EVERY space here — a
                        # space-qualified spec ("<sid>:name@vN", e.g. the
                        # ADR-013 probe / the compose render) resolves to
                        # the same bare-name source
                        src = local_source(spec.split(":")[-1], programs_dir)
                out = {"objectId": spec, "marker": marker, "source": src}
            elif name in ("span.begin", "span.end"):
                out = {}
            elif name == "test.any":
                fn = getattr(any_client, payload["method"])
                try:
                    out = fn(*payload["args"], **payload["kwargs"])
                except Exception as e:  # -> shim AnyError, guest-catchable
                    out = {"__error__": f"{type(e).__name__}: {e}"}
                    # a fake raising with wire attrs keeps them visible to
                    # guest code that branches on status/code
                    if hasattr(e, "status") and hasattr(e, "code"):
                        out["__status__"] = e.status
                        out["__code__"] = e.code
            elif name == "test.llm":
                out = llm_chat(**payload)
            elif name == "runtime.get" and payload.get("key") == "shell":
                if shell is not None:
                    out = {"value": shell}
                elif effect is not None:
                    out = effect(name, payload)
                else:
                    out = {"value": None}   # a binary without the feature
            elif effect is not None:
                out = effect(name, payload)
            else:
                raise AssertionError(f"unhandled effect {name}")
        except AssertionError:
            raise
        except Exception as e:
            return json.dumps({"ok": False, "error": {
                "type": type(e).__name__, "message": str(e)}})
        return json.dumps({"ok": True, "output": out})

    global _n
    _n += 1
    ww = types.ModuleType("wit_world")
    ww.host_effect = host_effect
    sys.modules["wit_world"] = ww
    # the vendored guest libs (bs4/markdownify…, ADR-012 §6) live beside
    # app.py — host-side import needs the dir on the path like wasm does
    if str(APP_PY.parent) not in sys.path:
        sys.path.insert(0, str(APP_PY.parent))
    spec = importlib.util.spec_from_file_location(f"_guest_app_{_n}", APP_PY)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app


def kernel_globals(now=0.0, offset_s=0):
    """The kernel's pure time globals (ADR-019 §1/§8) for harnesses
    that exec a program under a hand-made namespace: ts_s / instant /
    fmt_ts / tz_offset / now, backed by a fixed clock."""
    app = load_kernel(effect=lambda n, p: {"epoch": now, "offset_s": offset_s})
    return {"ts_s": app.ts_s, "instant": app.instant, "fmt_ts": app.fmt_ts,
            "tz_offset": app.tz_offset, "now": app.now}
