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
- [x] Config effect (ADR-006 §3): cascade localValue??value??default;
      secret enforcement; NUL-sanitization guard at the write boundary.
- [x] anyclient: typed `any` HTTP client (boring; grows in M4).
      BlobStore seam (FileSidecar now, AnyFile later).

Metrics checkpoint after M2 (ADR-003/005): review recorded numbers,
tune digest/ceiling defaults from data.


## M2 done (2026-07-08)

65 tests, ruff + pyright clean, all offline (no server, no key). anybao
app layer complete: digest, loop (ceilings/mailbox), llm effect +
3 adapters, config effect, anyclient, BlobStore seam.

### Metrics checkpoint

What is now MEASURED (recorded per run, was impossible with sobek):
- per-cell fuel_used, mem_pages, duration_ms (cell records, ADR-001 §4b)
- per-llm-call usage {in, out} (meta.usage) → token/cost totals
- per-effect durMs

What stays STUBBED pending LIVE data (the checkpoint's honest gap):
- **token counter** = ~4 chars/token approximation (digest.approx_tokens,
  pluggable via DigestPolicy.tokenizer). Calibrate against real
  meta.usage once a live conversation is recorded; swap to a
  provider tokenizer in the llm effect.
- **digest inline budget** (1000 tok) and **ceiling defaults** (108
  turns / 1M tokens) are starting values — tune from recorded
  distributions after the first real runs (M4 has a live server).
- cost accounting needs tier pricing in config (wired M4).

Conclusion: the measurement plumbing is in and correct; calibration is
deferred to M4 (first live traces) by design — decide-from-data, not
guess-now. No blocker for M3/M4.

Next: M2 review → M3 (any-side upstream: agentlog v2, history chunker,
drift bootstrap) ∥ M4 kickoff.
