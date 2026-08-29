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
| `oauth.connect(provider, scopes?, timeout?)` | mutate | oauth.connect | consent via loopback + PKCE (ADR-011 §5); blocks ≤120s, listener runs a 5-min window → `{ok, provider, grantedScopes, account}` — never token material |
| `oauth.status(provider)` | read | oauth.status | `{connected, pending, scopes, account, expiresAt}` — state only, no network |
| `oauth.disconnect(provider)` | mutate | oauth.disconnect | provider-side revoke (best-effort) + device-local delete → `{ok, provider, revoked}` |
| `oauth.refresh(provider)` | mutate | oauth.refresh | **host-emitted only** (ADR-011 §6): the refresh-token exchange recorded before the http record it serves; a direct guest call fails typed `host_only` |
| `trace.effects_of(cell?, span?, run?)` / `trace.effect_get(seq, run?)` | read | — | agent-side trace views; `run` = a past run from the trace store (ADR-003 §4) |
| `trace.runs(program?, filter?, sort?, limit?)` / `trace.stats(run)` | read | — | the run finder over per-run summaries (any-store filter/sort, ADR-023 §5) + per-run cost/shape summary |
| `trace.query(pipeline, coll?)` | read | — | read-only aggregation over `records` / `runs` / `blobs` of this bao's trace store (ADR-023 §5); sinks refused |
| `span.begin/end` | — | — | guest-declared grouping (broker machinery, not registry effects) |

Route classification (`anybao.routes`, scoped to the any server's base
URL): GET → read; any-API POST `/query`, `/objects/query`, `/search`,
`/aggregate` → read `data.read`; other any-API writes → mutate
`data.write`; LLM completion endpoints (`/v1/messages`,
`/chat/completions`) → read `llm.chat`; everything else → `net.http`.

Credential injection: `credential: {ref, header, prefix?}` — the host
resolves `ref` and sets the header AFTER the payload is recorded;
values never reach guest memory or the trace. Static refs
(`connector.key.*`, `llm.key.*`) read the stored secret as-is; managed
refs (`connector.oauth.<provider>`) resolve through the host-held
token lifecycle — cached access token, refresh-at-injection with a
120s margin (ADR-011 §6).
