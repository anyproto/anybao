# M1 — runtime core (anyrt complete) — ACCEPTED 2026-07-08

Scope from docs/01-implementation-plan.md; ADRs 001–004. Checklist
(one commit per item, roughly):

- [x] Guest namespace v1 (ADR-002 §3/§4): curated builtins, import
      allowlist hook, proxied datetime/random/time/os.environ, shim
      globals (`now()/rand()/env()`, effect-backed `uuid4`), http
      facade (`http.get/post -> Response`), PYTHONHASHSEED=0 +
      `kernel.boot` record (pinned determinism facts).
- [x] Built-in nondeterminism effects (runtime-owned): `time.now`,
      `random.random`, `uuid4`, `sleep`, `env.get`.
- [x] values half of the kernel API (guest-side store, real objects,
      `values.get(cell, i|"last")`, reset clears).
- [x] effects half: `effects.of(cell)` / `effects.get(seq)` as trace
      views reachable from the guest.
- [x] Trace v2 completion: blob spill (64KB knob), traceDiff/callTrace
      views.
- [x] `*_many` fan-out in the broker (input-order records).
- [x] `use()` + frame chain + probe cache (pluggable resolver effect;
      any-backed resolver arrives M2/M4).
- [x] Golden fixture regenerated to the facade idiom (twice —
      the second regen was the kernel.boot pin catching a kernel edit,
      the divergence mechanism observed live).

Deferred/known: WASI-level virtual clock isn't exposed by wasmtime-py's
add_wasip2 — interpreter-internal clock reads stay real (harmless:
cells only see proxied time); revisit upstream. Hash determinism via
PYTHONHASHSEED env instead.


## M1 done (2026-07-08)

All items complete; 35 tests green, ruff + pyright clean. anyrt is the
full runtime contract. Key learnings:
- componentize-py bundles only STATICALLY-visible stdlib imports — the
  literal import block at the top of app.py is load-bearing; extending
  the tier-1 allowlist means adding both an `_ALLOWED` entry and a
  literal `import`.
- pyright dislikes ModuleType attribute assignment; proxies use
  types.SimpleNamespace (identical behavior).
- The kernel.boot sha256 pin tripped strict replay on every guest edit
  (fixture regenerated ~4×) — the divergence tripwire works as designed.
- PYTHONHASHSEED=0 via WasiConfig.env pins guest str-hash determinism
  (wasmtime-py add_wasip2 exposes no virtual clock; interpreter-internal
  time reads stay real but cells never see them — only the proxies).
- Batch/use() executes host-side sequentially in M1; the RECORD contract
  (input-order, batch tags, self-contained resolve records) is the part
  that matters — concurrency is a later optimization behind it.

Next: M1 review → M2 (harness core: anyclient, llm effect + adapters,
config effect, loop.py/digest.py with ceilings/mailbox/budgeted digest).
