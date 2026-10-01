# Experimental local AI transport for Bao

Only `toolcaller@v2` and `llm@v2` live here. Deploy this folder to a private
`local_ai` overlay, never to the shared `agent` space (ADR-030).

Keep the official `agent` and `connectors` overlays configured. Standard tools,
history and skills load from those live, synced spaces; there are no copies here.
Select `agent.program = "local_ai:toolcaller@v2"` and configure the explicit
`llm.tier.local_codegen` provider (`any-ai`); omit harness/model/effort/speed to follow
the device defaults, or set them to pin that tier. The experimental loop remains separately
versioned and does not automatically inherit changes to the official loop.

Local media helpers reuse the same resolved host route:

- `llm.read(file, question, tier="local_codegen")`: Any file URI, Blob or explicit
  inline data; PNG/JPEG/GIF/WebP (5 MiB) and UTF-8 text/JSON (512 KiB). Binary
  documents such as PDF/Office need explicit conversion first.
- `llm.search(query, tier="local_codegen")`: list of grounded answer strings with
  source links through Codex or Claude, no paid search-provider fallback.
- `llm.image_generate(prompt, tier="local_image")`: returns a Blob from the
  explicit Codex image route; use existing `attach_file` to place it in Any.
  Configure this tier separately (`provider: any-ai`, `harness: codex`,
  `timeout_ms: 240000`, `max_output_bytes: 8388608`). Claude can remain the text
  default. Image requests pin Standard: controller speed does not select the
  image generator's speed. Missing/unsupported image routes fail without fallback.

These are separate recorded calls, not native tools granted to the chat model.
Image bytes use the existing Blob/trace pipeline. The chat profile still rejects
direct File parts; attachments are read on demand with `read`. Effects record
model/usage and replay without a provider. Detailed support: any-ai ADR-003.

Chat-requested settings use the same device-local preferences as Settings:
`llm.settings()` reads them, `llm.models("codex")` lists advertised choices, and
`llm.select_model("codex", model="advertised-id", effort="high")` saves a
revision-checked default. Invoke the setter only on an explicit user request.
Omitted model/effort/speed retain saved choices; `None` clears an override (speed
returns to `standard`). `speed="fast"` requires an explicit model and informed
user consent: it uses higher Codex quota/credits or Claude's separately billed
usage credits. Inspect `supported_speeds`; it is not entitlement or guaranteed
effective speed. Never switch model/provider to obtain Fast. The current
run stays frozen; future runs follow the new default unless their tier is pinned.
These are recorded operations: replay never repeats the preference write.
