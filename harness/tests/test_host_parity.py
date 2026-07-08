"""Host parity (`-m integration`): the Rust host and the Python
reference host run the SAME guest program against the SAME live server
and must produce structurally identical traces — same effect
name+class sequence, same span names, same cell verdict. The Python
host's traces define the contract; this test is the Rust host's gate.

Skips unless host/target/{debug,release}/anybao-host exists
(`make host`)."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from anybao.config import Config, DictConfigStore
from anybao.modules import AnyModuleResolver
from anybao.runner import Runner

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "bin" / "kernel.wasm"


def rust_host() -> Path | None:
    for profile in ("release", "debug"):
        p = ROOT / "host" / "target" / profile / "anybao-host"
        if p.exists():
            return p
    return None


def shape(trace_path: Path) -> list:
    """The structural signature: (kind, name, class/phase/ok) per record,
    volatile fields (seq/durMs/fuel/outputs/keys-with-embedded-time)
    excluded."""
    out = []
    for line in trace_path.read_text().splitlines():
        r = json.loads(line)
        kind = r.get("kind")
        if kind == "header":
            out.append(("header", r["schema"]))
        elif kind == "effect":
            out.append(("effect", r["effect"], r["meta"].get("class")))
        elif kind == "span":
            out.append(("span", r["name"], r["phase"]))
        elif kind == "cell":
            out.append(("cell", r["cell"], r["ok"]))
    return out


def test_rust_and_python_hosts_trace_identically(client, fresh_space, any_server):
    binary = rust_host()
    if binary is None:
        pytest.skip("no anybao-host binary — run `make host`")
    if not KERNEL.exists():
        pytest.skip("bin/kernel.wasm missing — run `make kernel`")

    # a brain query with no items: decay@v1 sweeps nothing — cheap, no LLM
    brain = client.get_brain(fresh_space)["objectId"]
    args = {"space": fresh_space, "brainId": brain}
    scratch = Path(tempfile.mkdtemp())

    # --- the Rust host -------------------------------------------------------
    config = scratch / "config.json"
    config.write_text(json.dumps({"any.base_url": any_server}))
    rust_traces = scratch / "rust"
    proc = subprocess.run(
        [binary, "decay@v1", "--args", json.dumps(args),
         "--kernel", KERNEL, "--programs", ROOT / "programs",
         "--traces-dir", rust_traces, "--config", config],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rust_out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert rust_out["value"] == {"swept": 0, "updated": 0}
    rust_trace = rust_traces / f"{rust_out['traceRef']}.jsonl"

    # --- the Python reference host ------------------------------------------
    cfg = Config(DictConfigStore({"any.base_url": {"value": any_server}}))
    runner = Runner(client, cfg, kernel_wasm=KERNEL,
                    traces_dir=scratch / "py",
                    resolver=AnyModuleResolver(client, current_space=fresh_space),
                    user_space=fresh_space, any_base=any_server)
    # the local-dir resolver equivalence: deploy is covered elsewhere;
    # here both hosts must read the SAME sources
    from anyrt.builtin_effects import DictResolver
    runner._resolver = DictResolver({p.stem: p.read_text()
                                     for p in (ROOT / "programs").glob("*.py")})
    py = runner.run_program("decay@v1", args, mailbox=None)
    assert py.status == "ok", py.error
    assert py.value == {"swept": 0, "updated": 0}
    py_trace = scratch / "py" / f"{py.trace_ref}.jsonl"

    rust_shape, py_shape = shape(rust_trace), shape(py_trace)
    # the Python host records a per-effect input key; both compute the
    # same sha256 domain — spot-check one shared record end-to-end
    assert rust_shape == py_shape, f"\nrust: {rust_shape}\n  py: {py_shape}"

    def keys(p):
        return [json.loads(ln).get("key") for ln in p.read_text().splitlines()
                if json.loads(ln).get("kind") == "effect"
                and json.loads(ln)["effect"] == "module.resolve"]

    assert keys(rust_trace) == keys(py_trace)  # input_key hash parity
