# ADR-002: Effect boundary & isolation

Status: **Accepted** (2026-07-07)
Date: 2026-07-07
Builds on: ADR-001 (trace format v2 — accepted)

## Context

The isolation principle (00-plan.md §5): nothing executes side effects
except through the effect boundary — one invariant, two faces (bit-exact
replay + confinement). ADR-001 fixed what a recorded effect *looks
like*; this ADR fixes how effects are **declared, called, checked, and
how the cell namespace is built** so that no other path to the world
exists. Out of scope here: executor mechanics (ADR-003), module
resolution grammar (ADR-004), the concrete effect catalog (fetch/llm/
chat/… specified as built, each a small doc-per-effect).

## Decision

### 1. Declaration: the `@effect` decorator

Effects are host-side functions (anyrt/anybao code — never guest code),
registered declaratively:

```python
@effect("http.get", kind="read",            # JSON trace field: "class"
        normalize=normalize_http,           # raw args -> canonical input (ADR-001 §3)
        redact=["headers.authorization"],   # sensitive paths, masked pre-record
        cap="net.http")                     # required capability (default: effect name)
def http_get(ctx, url, *, params=None, headers=None, timeout=None): ...
```

**Pythonic surface (not JS land).** The old harness mimicked JS
(`fetch`, `console.log`); anybao's effect catalog mimics Python idioms —
the model has seen far more requests-style Python than JS-in-Python:
`http.get/head/post/put/patch/delete(...)` with `params=/headers=/json=`
(plus `redirects=` — max follows for the request, `0` = manual; and
`response="base64"` — the body comes back as base64 of the raw bytes,
ADR-020 §1; `stream=True` reads the response as it arrives, the
recorded `body` is the raw SSE text, still ONE record; `timeout=` is a
whole-request cap in seconds, default 180, or `{idle, total}` — a
stream may go `idle` s without a chunk and `total` s overall, defaults
60 / 900, both ending as `URLError` — BOB-149)
returning a `Response` (`.status`, `.json()`, `.text`, and `.url` — the
final post-redirect url; both added by ADR-008 §2; `head` classifies as
`read` like `get`, `patch` as `mutate` — added 2026-07-28 for the REST
connectors); **`print()` is the
model-facing output channel** (the console.log role — traced as a
structured-value record, primary input to the digest); snake_case
everywhere. Granular verbs also fix classification: `http.get` is
`read`, `http.post` is `mutate` — one JS-style `fetch` couldn't declare
either honestly.

- `kind` is declared, never guessed (kills the v1 name-prefix
  heuristic). `kind="read"` effects are safe to re-execute; `mutate`
  are not — replay and future tooling rely on this.
- `normalize` produces the canonical input; identity+canonical-JSON if
  omitted. `redact` masks before the record is written — secrets
  structurally cannot enter traces (ADR-001 §3).
- Inputs and outputs MUST be JSON-serializable (trace constraint); the
  decorator enforces this at call time, loudly.
- `ctx` is the broker handle, visible only to effect implementations:
  credential/config resolution happens here, inside the boundary —
  guest code passes tier names/handles, never key bytes.

### 2. The broker: one pipeline for every call

Every effect call, no exceptions, flows through:

```
normalize → key → capability check → replay/mock consult → execute → record → return
```

- **Capability check** precedes execution. The active grant set is the
  Macaroons-style **intersection over the import chain** (a program can
  never lend more authority than it holds; frame tracking is supplied
  by the executor/loader, the *rule* lives here). Denials are recorded
  as error records (`error.type = "capability_denied"`) — audit for
  free.
- **Replay/mock consult** per ADR-001 §5 (strict cursor / loose FIFO).
  Mocked calls skip execute but still record (`mocked: true`).
  The loose index is either run-level (`anyrt run --mock`) or
  **span-scoped** (amendment 2026-09-14, ADR-028 §3): a `span.begin`
  whose input carries `mock` installs an index for the span's
  lifetime, its `span.end` drops it, nested spans inherit the innermost;
  a bad spec fails the begin itself. `span.*` and `trace.*` are never
  served from a mock (ADR-028 §7) — the trace's own machinery must not
  lie about the run it is in.
- **Record always** — success, error, denial, mock. If it isn't in the
  trace, it didn't happen; corollary: if it happened, it's in the trace.
- Effect failures return as **data** in the record AND raise a typed
  `EffectError(seq, type, message)` into the guest — the cell may catch
  it; the digest shows it; nothing is silently swallowed.

**The enforcement mechanism ships enabled from day one.** v2.0 runs a
permissive default grant profile (self-authored context), but checks
execute, denials are possible, and every decision is recorded — policy
tightens later without touching mechanism.

### 3. The cell namespace: deny-by-default

Cells execute in a constructed namespace containing **only**:

- **kernel facades** (tools; ADR-003 owns their shape),
- **effect shims** (§4) and **`print()`** — the traced structured-value
  output channel (console.log's role in v1),
- **curated builtins**: the pure computation subset (`len`, `range`,
  `enumerate`, `zip`, `sorted`, `min/max/sum`, `dict/list/set/tuple`,
  `str/int/float/bool`, `isinstance`, `repr`, comprehension machinery,
  exceptions). Excluded: `open`, `input`, `eval`, `exec`, `compile`,
  `breakpoint`, `globals`, `vars`, raw `__import__` (replaced by our
  hook), `memoryview`/`bytearray` stay (pure), `object.__subclasses__`
  escape hatches are *not* chased in-process — see §5.
- **the import hook** (§4) as the only `__import__`.

### 4. Shims and imports: ambient authority replaced, not blocked

- **Module allowlist, audited once against the kernel's stdlib
  (amendment 2026-09-04, ADR-026)**, resolved by `_guest_import` —
  the cell namespace's only importer. Entries are dotted names
  (`urllib.parse` admits that submodule, not `urllib.request`). Every
  module the kernel image carries sits in exactly one tier with its
  reason, and one guest test imports every admitted name, exercises
  the proxied edges (a zip write, an in-memory sqlite table) and
  asserts every refused name fails with its pointer. Admission test,
  unchanged: deterministic, no ambient authority.
  1. *Pure stdlib* — passes through:

     | group | modules |
     |---|---|
     | data / text | `json`, `re`, `string`, `textwrap`, `unicodedata`, `difflib`, `csv`, `html`, `email`, `xml.etree`, `urllib.parse`, `tomllib`, `configparser`, `shlex`, `fnmatch`, `pprint`, `quopri`, `plistlib` |
     | numbers | `math`, `cmath`, `decimal`, `fractions`, `statistics`, `ipaddress`, `colorsys`, `calendar` |
     | containers / control | `itertools`, `functools`, `collections`, `contextlib`, `heapq`, `bisect`, `graphlib`, `operator`, `copy`, `dataclasses`, `enum`, `typing`, `abc`, `traceback` |
     | codecs / bytes | `base64`, `binascii`, `struct`, `array`, `zlib`, `gzip`, `zipfile`, `tarfile`, `hashlib`, `hmac` |
     | identity / chance | `uuid`, `random`, `secrets` — pure *because of the WASI floor below*: the stdlib seeds from the pinned entropy stream |
     | introspection | `inspect` (ADR-010 §2), `ast` (ADR-013 §3) |

     Not in the image at all (`bz2`, `lzma`, `ssl`, `ctypes`): the
     ImportError says so, and `zipfile`/`tarfile` degrade to their
     zlib-backed formats. The image holds what componentize-py's
     static analysis sees from `app.py`, so every admitted module is
     imported there literally — and so are the codecs those modules
     load lazily (`cp437` for zip entry names, `latin-1`/`ascii` for
     mail bodies): a zip read failed on `unknown encoding: cp437`
     until it was.
  1b. *Vendored pure-Python third-party* (ADR-012 §6): `bs4` +
     `soupsieve`, `markdownify` — bundled verbatim into the kernel
     image from `runtime/guest/` (versions + licenses in
     `runtime/guest/VENDORED.md`), importable through the same
     allowlist; bs4 runs on the stdlib `html.parser` backend (no
     lxml). Their internal deps (`six`, `typing_extensions`) are
     bundled but NOT guest-importable — the allowlist names only the
     supported surface.
  2. *Proxied stdlib* — the API is wanted, the ambient part is
     replaced by an effect or a blob:

     | module | proxy |
     |---|---|
     | `datetime` | construction/arithmetic pass through; `datetime.now()`, `date.today()` are effect-backed → `time.now` records |
     | `time` | `time()`/`sleep()` effect-backed |
     | `os` | `os.environ` as the `env.get` effect; `os.fspath`/`os.PathLike` pass through (the archive modules need them) |
     | `io` | pass through minus `open`, `open_code`, `FileIO` — the three that take a path |
     | `tempfile` | `TemporaryFile`/`NamedTemporaryFile`/`SpooledTemporaryFile` return the blob writer (ADR-026 §4): a file-like object that becomes a Blob on close; `TemporaryDirectory`/`mkdtemp` refused pointing at ADR-024 |
     | `sqlite3` | `connect(":memory:")` only — an in-memory database is pure; a path or `uri=True` is refused, and `Connection` (whose constructor takes a path) is not exposed |
     | `mimetypes` | the built-in table only: `knownfiles` emptied and `init()` run once at proxy build, `init` not exposed |

  Refused with a pointer, not a bare error: `pathlib`, `shutil`,
  `glob`, `os.path` → the ADR-024 `fs.*` effects; `socket`, `select`,
  `subprocess`, `threading`, `multiprocessing`, `asyncio`, `signal` →
  the effect boundary; `urllib.request`, `http.client`, `ftplib`,
  `smtplib` → `http`; `pickle` → `json` (code execution on load, no
  use case).
  3. *Space programs* — `name@version` per the resolution rules
     (ADR-004); each load emits a `module.resolve` record.
  Everything else: `ImportError` with a message naming the boundary.
- Direct shims also injected as globals for ergonomics (`now()`,
  `rand()`, `env(...)`) — same effects underneath.
- **Import is an effect**: allowlist decisions and resolved versions
  are part of the recorded run.
- **The WASI floor (amendment 2026-09-04, ADR-026).** The allowlist
  gates what *cell code* imports; a module's own imports resolve
  through the real importer, so `zipfile`'s `time.localtime()` or
  `random`'s import-time seeding reach the WASI clock and entropy,
  not a proxy. The host therefore pins the WASI context per run —
  the host runs one cell per run (ADR-001 §4b), so the run is the
  cell: the wall clock is the header's `startedAt` (frozen for the
  run), the monotonic clock a counter advancing 1 µs per read, and
  both random sources xoshiro256** seeded (splitmix64) from the
  header's 32-byte `seed` and a per-source tag — the generator is
  pinned in the runtime, not borrowed from a crate, so a recorded run
  replays on any anyrt version (ADR-001 §2/§4: one record per run,
  not one per draw). `rand()` is the stdlib `random.random` over that
  stream; the `time` proxy overrides every "the present" spelling —
  `time()`, `monotonic()`, `perf_counter()` and their `_ns` forms —
  with the recorded `time.now`, and `sleep()` with the `sleep` effect,
  and passes the rest of the module — `gmtime`/`localtime`/`strftime`
  — through to the floor: a cell timing itself gets a real, replayable
  delta; a library stamping an archive gets the run's start.
  Admission rule that follows: a module whose *internals* run a
  time-based loop or sleep (a retry deadline, a poll) sees a clock
  that never advances and needs a proxy, never pass-through — none of
  the tier-1 modules has one, they only stamp. Consequences: `random`, `secrets` and `uuid` need no proxy —
  the stdlib seeds from the pinned stream and a `shuffle` of ten
  thousand items is zero trace records; archives carry deterministic
  timestamps; module-internal ambient calls are replay-safe by
  construction. The model-facing API for the present is still the
  recorded effect — `now()` is the real current time and a record,
  `rand()`/`uuid4()` stay as sugar — the floor is a determinism
  guarantee, not a clock the guest is meant to read.
  `PYTHONHASHSEED=0` is the same pin for hashing.

### 5. Enforcement strength is staged; the API is not

In-process v2.0 delivers this contract for honest code: nothing ambient
is *reachable*, so accidental effects and accidental nondeterminism are
structurally impossible. Deliberate escape (ctypes-style) is not
defensible in-process and we do not pretend otherwise — that is the
security milestone (a static Rust host embedding wasmtime implements
the SAME syscall surface natively; nothing above the boundary changes).

## Design rationale: the thin host (syscall doctrine)

**Syscall analogy, taken literally.** The host is a kernel: it exposes
a SMALL, STABLE syscall set — `http.*` (the one outbound door),
`config.get`, `mailbox.drain`, time/uuid/sleep/env,
`module.resolve`, `batch`, `trace.*` views, span begin/end — plus the
broker machinery (trace, replay, capability check, redaction) and the
engine itself. Everything else — the any client, llm adapters, memory
policy, recall, history, the conversation loop itself — is guest-side
Python: modules in program space, deployed and versioned like any
program, hot-swappable without touching the host. The host surface
changes rarely and by necessity; the product changes weekly WITHOUT a
host release.

**What the host contributes is exactly what guest code must not hold:**

- **Classification truth.** read/mutate and the capability for an http
  call derive from (method, url) at the boundary — a route table
  (anybao.routes, any_base-scoped), data not code, sourced from the
  api-drift manifest when routes change. Spans let guest facades group
  their calls into legible trace lines, but spans are narrative; class
  and cap are boundary facts guest code cannot fake.
- **Secrets.** Named-credential injection: a payload names
  `{ref, header, prefix}`; the host resolves the config secret and sets
  the header AFTER the payload records. Key values never enter guest
  memory or the trace — so llm adapters can live guest-side while keys
  never do.
- **Recording and replay.** The broker records every crossing; loop
  control (mailbox.drain) is itself a recorded effect, so a replayed
  conversation replays its interruptions.

**The two wraps answer different questions.** The guest-side facade
(module functions, `http.get`, `print()`) provides *shape*: a natural
Python surface for LLM-written code, holding ZERO authority — bypassing
it gains nothing. The host-side broker provides *authority and truth*.
Between them sits a serialized crossing: a wasm host call today, a Rust
host function at the security milestone. Only the transport changes;
neither wrap's contents do — that is how "identical program-facing
contract at every stage" is achieved mechanically, not by promise.

One line: **the cage and the syscalls are infrastructure and never
change; everything the agent IS lives above the boundary as deployed
guest code — so the host can be wasmtime-py today and a static Rust
binary tomorrow, and the agent cannot tell.**

## Consequences

- One choke point: tracing, mocking, capability checks, redaction, and
  limits all hook the same pipeline — each future feature is a broker
  middleware, not a new mechanism.
- The effect catalog becomes declarative data (name/kind/cap/normalizer)
  — auditable, diffable, and the natural source for the capability
  manifest in the program format.
- Guest code is portable across enforcement stages by construction.
- Cost accepted: proxy modules are fiddly to build correctly (datetime
  especially); the tier-1 allowlist will need occasional additions.

## Resolved questions (review 2026-07-07)

1. **Ergonomic shim names** — inject `now()/rand()/env()` globals in
   addition to proxied modules; both routes hit the same effects.
2. **`uuid`** — effect-backed `uuid4()` (recorded);
   deterministic uuid5 passes through the allowlist.
3. **Batch** — N individual records + host-side concurrency, exposed as
   **per-facade `*_many` methods** (no generic combinator in v2.0):
   `http.get_many(urls, ...)`, `llm.complete_many(prompts, tier=...)`.
   One guest→host crossing carries the list; the broker fans out to the
   same single-item effect on a pool (per-item cap checks/normalizers/
   redaction unchanged, no separate batch effect in the catalog).
   Records append in **input order** regardless of completion order
   (strict replay stays deterministic under concurrency), each
   individually mockable, `meta.batch: {id, i}` for provenance.
   Per-item failure is an `EffectError` value in that slot, never a
   whole-batch exception. Generic thunk-based `batch()` rejected:
   thunks are guest code and cells stay single-threaded; a
   descriptor-based combinator can be added later without touching the
   trace contract.
   *Teaching burden (accepted as low)*: not using `*_many` is
   correct-but-slow, never wrong — sequential calls yield the same
   records; real consumers are programs (classify maps, fan-outs), not
   ad-hoc cells. Guidance = tool docs + one skill line; plus a **digest
   hint** — when a cell loops the same effect many times sequentially,
   the result digest suggests `*_many` (teach at the moment it matters,
   zero standing prompt cost).
