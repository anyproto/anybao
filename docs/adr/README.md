# ADR index

Working rule: no code lands ahead of its accepted ADR. One ADR in
review at a time; user accepts explicitly.

| #   | Title                          | Status    |
|-----|--------------------------------|-----------|
| 001 | Trace format v2                | Accepted  |
| 002 | Effect boundary & isolation    | Accepted  |
| 003 | Executor & kernel API          | Accepted  |
| 004 | Module loading & resolution    | Accepted  |
| 005 | Loop core                      | Planned — neutral message model + provider adapters, ceilings, mailbox break/inject, digest policy (incl. orientation summaries) |
| 006 | Data contracts                 | Planned — turns/chunks v2 (server-assigned seq, chunk→child pointers), config object, trigger schema |
| 007 | Memory & graph write policy    | Planned — when/what the harness memorizes and links: save/dedup discipline, object-vs-property-edge decisions, edge vocabulary curation, interconnection maintenance (bird's-eye accuracy), and the query idioms that exploit it (recall paths, neighbor expansion). The POLICY layer over plan §4b/§4c mechanisms — the layer whose absence was the old system's biggest memory failure. |

The spike (loop skeleton + wasi executor + golden replay test) is
unblocked by 001–003; 005–007 can be reviewed in parallel with spike
implementation.
