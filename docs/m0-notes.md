# M0 spike — results & numbers

Date: 2026-07-07. Status: **exit criteria met** (pending review).

## Exit criteria (01-implementation-plan.md)

1. ✅ Cell executes in the guest; effects cross the boundary and record
   in trace v2 shape — `test_cell_executes_persists_and_effects_record`
   (persistence across cells, print capture, last-value, effect record
   attributed to its cell, fuel metrics in the cell record).
2. ✅ Strict replay of the golden conversation passes end-to-end
   (`test_golden_replay_strict`, cursor exhausted = no missing calls);
   a tampered trace raises `DivergenceError`
   (`test_golden_replay_divergence_on_tampered_trace`). Broker-level
   replay/divergence/mock-FIFO covered in `test_effects.py`.
3. ✅ Limits, each demonstrated: fuel exhaustion traps a `while True`
   (`test_fuel_exhaustion_interrupts`); epoch deadline lands inside a
   busy loop (`test_epoch_timeout_interrupts_busy_loop`); memory cap
   surfaces as a guest `MemoryError` with the kernel namespace
   surviving (`test_memory_cap_yields_memoryerror_kernel_survives`).
4. ✅ Numbers (Linux x86_64, this workstation):

| Metric                          | Value        |
|---------------------------------|--------------|
| componentize-py guest build     | ~1.4 s       |
| kernel.wasm size                | ~19 MB       |
| component compile (per process) | ~740 ms      |
| instantiate (per conversation)  | **~4 ms**    |
| trivial cell round-trip         | **0.2–0.9 ms** |
| cell w/ effect crossing         | ~1 ms        |
| fuel, trivial cell              | ~0.7 M units |
| full test suite (23 tests)      | ~2 s + one-time compile |

## Architecture as validated

componentize-py bundles CPython-for-wasi into a component; `host-effect`
is a real host function (`wit/kernel.wit`), implemented by the broker.
`add_wasip2()` + a `WasiConfig` granting ONLY stderr — no fs, no env,
no network exist in the guest's world. Fuel + epoch (10 ms ticker
thread) + `set_limits(memory_size=…)` all work through wasmtime-py as
ADR-003's appendix described.

## Learnings / M1 carry-overs

- wasmtime-py host funcs receive `(store, *args)`; a `WasiConfig` must
  be set on the store or instantiation panics in the C API.
- Cell records must be cursor-consumed in replay (broker.cell_done) —
  found by the golden test, exactly what it's for.
- Guest hash randomization is live entropy (wasip2 random) — fixture
  cells avoid order-sensitive reprs (sets); M1's deterministic-seed
  virtualization (ADR-003 kernel.boot) removes the caveat.
- Volatile fields (durMs, fuel_used, duration_ms) are normalized in
  fixture comparison; fuel is *nearly* deterministic but gc/hash
  effects can wiggle it — revisit once the boot seed is pinned.
- M0 guest keeps default builtins (the cage already denies the world);
  namespace curation + datetime/random proxies + values/effects kernel
  API are M1 scope, per plan.
- **`effect('http.get', {...})` in cells is NOT the final surface** —
  it's the raw single channel (the host-effect crossing). The
  cell-facing contract is ADR-002's Pythonic facades
  (`http.get(url) -> Response` with `.status/.json()/.text`), landing
  in M1 with the namespace work; facades marshal onto this same
  channel. Fixture cells will be rewritten to the facade idiom then
  (UPDATE_GOLDEN regen).
