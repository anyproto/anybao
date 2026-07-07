# M1 — runtime core (anyrt complete)

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
- [ ] effects half: `effects.of(cell)` / `effects.get(seq)` as trace
      views reachable from the guest.
- [ ] Trace v2 completion: blob spill (64KB knob), traceDiff/callTrace
      views.
- [ ] `*_many` fan-out in the broker (input-order records).
- [ ] `use()` + frame chain + probe cache (pluggable resolver effect;
      any-backed resolver arrives M2/M4).
- [x] Golden fixture regenerated to the facade idiom (twice —
      the second regen was the kernel.boot pin catching a kernel edit,
      the divergence mechanism observed live).

Deferred/known: WASI-level virtual clock isn't exposed by wasmtime-py's
add_wasip2 — interpreter-internal clock reads stay real (harmless:
cells only see proxied time); revisit upstream. Hash determinism via
PYTHONHASHSEED env instead.
