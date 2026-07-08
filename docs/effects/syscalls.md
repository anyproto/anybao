# The syscall surface

Doc-per-effect (ADR-002). This is the ENTIRE host effect catalog — the
frozen kernel API. Everything above it (any client, llm, memory,
recall, history, the toolcaller loop) is guest modules in `programs/`.

| Syscall | Kind | Cap | What |
|---|---|---|---|
| `http.get/post/put/patch/delete(url, params?, headers?, json?, body?, timeout?, credential?)` | route-derived | route-derived | the one outbound door → `{status, headers, body}` |
| `config.get(key)` | read | config.get | non-secret config (secrets resolve only inside syscalls) |
| `mailbox.drain()` | read | mailbox.read | loop control as a recorded effect |
| `time.now` / `random.random` / `uuid4` / `sleep` / `env.get` | read | — | determinism pins |
| `module.resolve(spec, frm?)` | read | — | `use()` resolution (ADR-004) |
| `batch(name, payloads)` | per-item | per-item | fan-out with input-order records |
| `trace.effects_of(cell?, span?)` / `trace.effect_get(seq)` | read | — | agent-side trace views |
| `span.begin/end` | — | — | guest-declared grouping (broker machinery, not registry effects) |

Route classification (`anybao.routes`, scoped to the any server's base
URL): GET → read; any-API POST `/query`, `/objects/query`, `/search`,
`/aggregate` → read `data.read`; other any-API writes → mutate
`data.write`; LLM completion endpoints (`/v1/messages`,
`/chat/completions`) → read `llm.chat`; everything else → `net.http`.

Credential injection: `credential: {ref, header, prefix?}` — the host
resolves `ref` via config (secret) and sets the header AFTER the
payload is recorded; values never reach guest memory or the trace.
