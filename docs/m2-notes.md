# M2 — harness core (anybao app layer)

Scope: ADR-005 + config. Checklist (one commit per item, roughly):

- [x] Digest v1 (ADR-005 §4): budget-aware inline/stub (tokens),
      sections (Output/Last/Side effects), teaching hints, orientation-
      summary HOOK (wired to llm once it exists). Needs per-value
      size/schema from the guest (enrich the run-cell reply to the
      ADR-003 ValueRef shape).
- [x] Loop v1 (ADR-005 §2/§3): ceilings→wrap-up turn, mailbox
      (inject/break soft+hard), turn-record shaping (deferred write
      until anyclient), progress bubbles.
- [x] llm effect + adapters (translation offline-tested; one-real-call
      seeding documented in docs/llm-fixtures.md) (ADR-005 §1): neutral message model;
      anthropic / openai-compat / fenced. Translation is pure +
      offline-testable; the HTTP call is the effect.
- [ ] Config effect (ADR-006 §3): cascade localValue??value??default;
      secret enforcement; NUL-sanitization guard at the write boundary.
- [ ] anyclient: typed `any` HTTP client (boring; grows in M4).
      BlobStore seam (FileSidecar now, AnyFile later).

Metrics checkpoint after M2 (ADR-003/005): review recorded numbers,
tune digest/ceiling defaults from data.
