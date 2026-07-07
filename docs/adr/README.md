# ADR index

Working rule: no code lands ahead of its accepted ADR. One ADR in
review at a time; user accepts explicitly.

| #   | Title                          | Status    |
|-----|--------------------------------|-----------|
| 001 | Trace format v2                | Accepted  |
| 002 | Effect boundary & isolation    | Accepted  |
| 003 | Executor & kernel API          | Accepted  |
| 004 | Module loading & resolution    | Accepted  |
| 005 | Loop core                      | Accepted  |
| 006 | Data contracts                 | Accepted  |
| 007 | Memory & graph write policy    | Accepted  |

**Design phase complete (2026-07-07): all seven ADRs accepted.**
Next: the spike — loop skeleton + WasiEngine (wasmtime-py +
python.wasm + effect host functions) + one golden replay test from a
converted real-conversation trace, offline in CI.
