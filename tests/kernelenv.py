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


class _Client:
    def __getattr__(self, name):
        def _call(*args, **kwargs):
            out = effect("test.any", {"method": name, "args": list(args),
                                      "kwargs": kwargs})
            if isinstance(out, dict) and "__error__" in out:
                raise AnyError(0, "test", out["__error__"])
            return out
        return _call


def client(base_url=None):
    return _Client()
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


def load_kernel(effect=None, any_client=None, llm_chat=None, programs_dir=None):
    """A fresh kernel app module wired to test fakes. `effect(name,
    payload) -> output` serves pass-through effects; `any_client` /
    `llm_chat`, when given, shadow the real any@v1 / llm@v1 modules.
    `programs_dir` overrides the source root (another repo's tests —
    e.g. repos/_connectors — resolve their own programs/)."""
    def host_effect(name, payload_json):
        payload = json.loads(payload_json)
        try:
            if name == "module.resolve":
                spec = payload["spec"]
                if spec == "any@v1" and any_client is not None:
                    src = ANY_SHIM
                elif spec == "llm@v1" and llm_chat is not None:
                    src = LLM_SHIM
                else:
                    src = local_source(spec, programs_dir)
                out = {"objectId": spec, "marker": "m0", "source": src}
            elif name in ("span.begin", "span.end"):
                out = {}
            elif name == "test.any":
                fn = getattr(any_client, payload["method"])
                try:
                    out = fn(*payload["args"], **payload["kwargs"])
                except Exception as e:  # -> shim AnyError, guest-catchable
                    out = {"__error__": f"{type(e).__name__}: {e}"}
            elif name == "test.llm":
                out = llm_chat(**payload)
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
    spec = importlib.util.spec_from_file_location(f"_guest_app_{_n}", APP_PY)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app
