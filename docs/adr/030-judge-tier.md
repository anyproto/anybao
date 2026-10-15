# ADR-030: The judge tier — typed questions over a state

Status: **Accepted** (2026-10-15)
Date: 2026-10-14
Builds on: ADR-002 §1/§2 (http effect, route classification),
ADR-005 §1 (tiers, tables, retry), ADR-021 §1/§8.1 (credentials,
host-bound refs), ADR-028 (mock mode — a facade span is mockable)
Amends: ADR-005 §1 (a fourth tier, `judge`, and a second call shape
next to `chat`)
Tracks: Linear BOB-158 (the zone survey: ten candidate call sites)

## Context

Most of anybao's small model calls are not generation. They ask a
bounded question and parse JSON back: memory dedup (merge / supersede
/ create), linkgen (is this a real relation, which edge type),
extraction (is anything durable in this batch), enrich reconcile
(outcome, target, type). Others are skipped because a chat round trip
costs too much on the hot path: an autorecall relevance gate, and
classifying an incoming chat message to decide whether bao should act
on it and how.

A new class of model answers exactly that shape. TypeSafe's Jev (the
first "System One" model) takes a `state` and a map of typed
questions and returns a calibrated answer per question: `noul` (yes/no
as a probability), `choice` (one of ≤255 options with a distribution
and a confidence), `score` (2–10 rubric levels, a probability-weighted
value). It cannot write text. The questions are evaluated in parallel
against one ingest of the state, in 70–500 ms (a three-question probe
answered in 240 ms), at $0.042 per million input tokens with output
free. More vendors are expected to ship this shape, eventually behind
a common API; the contract below is the shape, with TypeSafe as its
first wire.

Using a `chat` tier for this means parsing free text into a verdict on
every call, with no confidence to gate on. A tier per caller
("memory", "gating", …) would multiply config rows that all hold the
same model. The split that matters is the **call shape**: generation
vs judgement.

## Decision

### 1. One tier, `judge`, and one call: `llm.judge(state, questions)`

```python
llm = use("llm@v1")
llm.judge_available(tier="judge") -> bool      # a judge model is configured
answers = llm.judge(state, questions, tier="judge")

state     = str | dict | list                  # the content judged; JSON-safe
questions = {id: Question}                     # ids are the caller's; never sent to the model as text
Question  = {"type": "noul",   "instructions": I, "criteria"?: {"true": I, "false": I}}
          | {"type": "choice", "instructions": I, "criteria": {option: I | None}}   # 1..255 options
          | {"type": "score",  "instructions": I, "criteria": [I, …]}               # 2..10 levels, ordered
I         = str | dict | list                  # structured instructions reference fields by `name`

→ {"answers": {id: Answer}, "model": str, "usage": {"in", "out"}}
Answer    = {"type": "noul",   "noul": p}                                           # p = P(yes)
          | {"type": "choice", "choice": option, "probabilities": {option: p}, "confidence": c}
          | {"type": "score",  "score": x, "probabilities": {"0": p, …}, "legend": {"0": I, …},
             "confidence": c}
```

The neutral shape **is** TypeSafe's wire shape: it is already typed,
vendor-neutral in vocabulary, and the closest thing to a standard the
class has. `judge` validates the questions before any call (unknown
type, missing `instructions`, a choice outside 1..255 options, a score
outside 2..10 levels, a non-dict `questions` → `ValueError`), so a
malformed question never costs a round trip or a 422.

`judge` is a `getter` facade (`llm.judge` span) over one `http.post`,
like `chat`. Callers ask many questions in ONE call when they share a
state: the vendor evaluates them in parallel, and it is one trace
record.

### 2. Judge wires live in their own table

The judge wire is a different family from the chat wires, so it gets
its own table, `JUDGES`, keyed by the tier row's `provider`:

```python
llm.tier.judge = {"provider": "typesafe",       # judge wire family
                  "model": "jev-latest",         # sent verbatim; pin a version id once thresholds are tuned
                  "base_url": "https://api.typesafe.ai",
                  "api_key_ref": "llm.key.typesafe",
                  "long_context": bool | absent, # TypeSafe-Danger-Zone-Long-Context: 262k instead of 64k
                  "timeout": n | absent,         # total seconds, default 30
                  "options": {…} | absent}       # raw request fields, merged last
```

An entry owns the URL path, the credential (header, prefix, ADR-021
`about`), request building and response normalization to §1's Answer.
A `chat` provider on the judge tier, or `typesafe` on a chat tier, is
a `ConfigError`. A second vendor, or a common judge API, is one more
`JUDGES` entry. When two hosts share one wire, the entry splits into
adapter + backend the way §1.1 of ADR-005 does; until then that is
premature.

### 3. No seed, no fallback: consumers branch on availability

`llm.tier.judge` is NOT in the config defaults. A seeded row would
name `llm.key.typesafe`, and every fleet without a TypeSafe key would
get a credential card for a feature nobody turned on. It would also
break the onboarding check, which compares every seeded tier
(`is_model_setup`).

`judge()` never falls back to a chat model. With no judge row it
raises `JudgeUnavailable` before any call. Emulating Jev on `classify`
was rejected for two reasons. It would give the high-volume usage a
judge model makes affordable (every item, on the hot path) at chat
prices and latency. It would also hide a misconfiguration behind
slower, costlier answers.

Consumers therefore branch once, on `judge_available()`. It reads the
tier row and makes no call.

- **Available (supercharged):** judge every item instead of a sample.
  Ask many questions per call, run where a chat round trip was too
  slow, and gate on calibrated confidence.
- **Not available (regular):** keep the consumer's existing
  `chat(tier="classify")` path, with the same batching and call volume
  as before this ADR.

A configured row whose key is missing still counts as available. The
first call raises the credential card (ADR-021 §2), the same as any
tier.

Turning it on is the row plus the key:
`config.set("llm.tier.judge", {…})`, and the key through the
credential card.

### 4. Credentials and the effect boundary

`llm.key.typesafe` is a host-bound SaaS ref (ADR-021 §8.1), declared
in `llm@v1.__any_credentials__` with `hosts: ["api.typesafe.ai"]`, so
a `base_url` edit cannot redirect it. It travels as `Authorization:
Bearer`, injected by the host after the payload records, like every
model key.

`/v1/systemone` joins the LLM path suffixes in the route classifier
(`runtime/src/routes.rs`). The call is therefore `read` and its
capability is `llm.chat`. It replays and mocks like any model call
(ADR-028: `only: ["llm.*"]` covers `llm.judge`). The request is plain
JSON, not streamed: the answer is a few dozen tokens, so there is no
idle gap to keep alive. The retry is ADR-005 §1.6 unchanged: 429 and
529 are TypeSafe's documented transient statuses, and 422 (a malformed
question that slipped past §1's validation) is not retried.

### 5. A judge is advice, never a guard

The vendor documents that Jev reads the state literally and follows
instructions planted in it. A judge answer therefore never decides
anything security-relevant. It routes, ranks and gates cost; it never
grants capabilities, skips confirmation, or classifies content as
"safe". English is the primary language. A caller that judges
non-English content treats a low confidence as "unknown", not "no".

### 6. Consumers are their own changes

This ADR adds the tier and the call, and no caller. Each consumer
lands with its own thresholds, measured against recorded traces (the
existing Haiku verdicts are the baseline, compared with `trace diff`).
The intended order:

1. **Chat message triage** (the motivating case): one judge call per
   incoming user message, deciding whether bao should act on it at all
   and which way. The questions and their wiring into the responder
   loop are a separate design.
2. Memory dedup (`memory@v1._judge`) and the autorecall relevance
   gate: the hot-path zones from BOB-158.
3. Linkgen and an extraction pre-gate: near-free cron wins.
4. Enrich reconcile and mail triage at sync time: product-visible,
   and each needs schema work first.

## Consequences

- A fourth tier row and a second call shape in `llm@v1`. `chat`, the
  toolcaller and every existing caller are unchanged.
- Without a judge tier, behavior and cost are exactly what they are
  today: consumers stay on their classify path. With one, a judgement
  costs a fraction of a Haiku call and returns in a fraction of the
  time, with confidence to gate on. That is what makes the
  supercharged mode affordable.
- Each consumer carries two paths (§3). The regular path is the code
  that exists today, so the second path is the only new code.
- `trace show --stats` counts tokens from `llm.chat` spans only. Judge
  usage sits on the `llm.judge` span end and is not in the totals yet.
- A single vendor until a second ships. A TypeSafe outage or a
  revoked key raises from `judge()`. A consumer on the hot path
  catches it and takes its regular path for that call, so the outage
  never becomes a bao outage.

## Open questions

- Confidence thresholds per consumer: tuned per zone, stored with the
  consumer, never a global trait.
