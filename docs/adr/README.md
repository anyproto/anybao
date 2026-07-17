# ADR index

Working rule: no code lands ahead of its accepted ADR. One ADR in
review at a time; user accepts explicitly.

**Documentation tiers** (agreed 2026-07-07): ADRs own the why and the
contract — the single canonical home; a review question revealing
missing rationale is fixed by an ADR amendment, never a code essay.
Code gets **one-line constraint pointers** (`ADR-00N §M`) that route
the reader without duplicating (duplication drifts). Point-in-time
learnings (perf numbers, stage caveats) go to milestone notes. Sole
long-comment exception: a non-obvious cross-system hazard at the exact
line a future reader would "fix" (e.g. the NUL separator note).

| #   | Title                          | Status    |
|-----|--------------------------------|-----------|
| 001 | Trace format v2                | Accepted  |
| 002 | Effect boundary & isolation    | Accepted  |
| 003 | Executor & kernel API          | Accepted  |
| 004 | Module loading & resolution    | Accepted  |
| 005 | Loop core                      | Accepted  |
| 006 | Data contracts                 | Accepted  |
| 007 | Memory & graph write policy    | Accepted  |
| 008 | Agent tools & credentials      | Accepted  |

**Design phase complete (2026-07-07): all seven ADRs accepted.**
Next: the spike — loop skeleton + WasiEngine (wasmtime-py +
python.wasm + effect host functions) + one golden replay test from a
converted real-conversation trace, offline in CI.
