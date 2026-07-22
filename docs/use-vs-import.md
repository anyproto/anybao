# `use()` vs `import` in the guest

Why guest programs load `any@v1` with `use("any@v1")` instead of a
Python `import`. The contracts live in the ADRs (ADR-002 §4, ADR-004
§2); this page is the cross-cutting explainer.

**`import` is the kernel's frozen world.** The guest replaces
`__import__` (`runtime/guest/app.py`, `_guest_import`) and serves
exactly two tiers: the pure-stdlib allowlist (`json`, `re`,
`contextlib`, …) and the proxied ambient modules (`datetime`,
`random`, `time`, `os`). Everything importable is baked into the
kernel wasm at componentize time — same bytes on every machine,
version fixed at kernel build, no I/O to load. That is why importing
gets to look like ordinary Python: it is deterministic by construction,
and there is nothing for the trace to record.

**`use()` loads code that lives in a space.** A program is an object,
not a bundled file; loading it means resolving which space, which
version, and whether the cached copy is still current (marker). That
resolution is host-mediated and nondeterministic — the answer can
change between runs, machines, and deploys — so under the isolation
principle it is an **effect** (`module.resolve`). ADR-002 §4 states it
directly: *import is an effect — allowlist decisions and resolved
versions are part of the recorded run.* Replay depends on this: the
trace pins exactly which source bytes ran, even if the space has since
moved on. Python's import statement has no seam for that; routing
space resolution through it would smuggle a network-shaped side effect
past the trace disguised as a language builtin.

Two semantics `import` cannot express:

- **Owner binding** (ADR-004 §2.4): every loaded module gets a `use`
  bound to its defining space, so *its* dependencies resolve where *it*
  lives — an overlay program is self-contained and never silently pulls
  deps from the consumer's space. Python has one global `sys.modules`;
  there is no "resolve relative to the space that owns this caller."
- **Freshness**: `use()` probes on every call and caches by
  `(objectId, marker)`, so a redeploy is live on the next call — the
  edit → `anyrt deploy` → next-conversation loop, all the way down the
  dependency chain. `import` caches once, for the process lifetime.
  (Same reason programs bind `use(...)` at call sites, not module top:
  a top-level binding would freeze the resolved module for the life of
  the kernel instance.)

Import-hook sugar (translating `import any_v1` into `use()`) is
deliberately not provided: an innocent-looking import would perform a
traced, failable, space-dependent effect, and the one-glance property
would be lost. The split is the boundary made visible:

| | `import x` | `use("x@vN")` |
|---|---|---|
| source | bundled into the kernel | an object in a space |
| version | fixed at kernel build | resolved per call, marker-cached |
| determinism | by construction | recorded (`module.resolve` effect) |
| dep resolution | global | bound to the owning space |
