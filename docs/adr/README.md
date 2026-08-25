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
| 009 | Space-resident assets, host config, overlays, lib mode | Accepted |
| 010 | Native introspection — docstrings as the single doc surface | Accepted |
| 011 | OAuth credentials — host-held tokens, `oauth.*` effects | Accepted |
| 012 | Gmail mailbox sync — space objects, email cleanup, contact graph | Accepted |
| 013 | Agent-authored programs — `create_program` in the working space | Accepted |
| 014 | Program progress — `progress@v1` over agent-progress objects | Accepted |
| 015 | Active-instance election — devices registry consumer | Accepted |
| 016 | Email corpus on runtime dataset schemas | Accepted |
| 017 | Agent data on userspace datasets — bundle-child stores | Accepted |
| 018 | Event triggers — chat sources, chat watcher as a trigger | Accepted |
| 019 | Instants — native dates in agent data and queries | Accepted  |
| 020 | File input — `any` files as model input, by reference | Accepted  |

**Design phase complete (2026-07-07): ADRs 001–007 accepted; 008
accepted 2026-07-17.** Implementation followed the
[milestone plan](../01-implementation-plan.md): M0–M6 all landed by
2026-07-08 (Rust `anyrt` runtime, wasm CPython guest kernel); ADR-008's
tool surface landed 2026-07-17. ADRs now evolve by amendment alongside
the code they govern — open work is tracked as Task objects in the
dev space, not here.
