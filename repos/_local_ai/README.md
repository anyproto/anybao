# Experimental local AI transport for Bao

Only `toolcaller@v2` and `llm@v2` live here. Deploy this folder to a private
`local_ai` overlay, never to the shared `agent` space (ADR-030).

Keep the official `agent` and `connectors` overlays configured. Standard tools,
history and skills load from those live, synced spaces; there are no copies here.
Select `agent.program = "local_ai:toolcaller@v2"` and configure the explicit
`llm.tier.local_codegen` model/harness. The experimental loop remains separately
versioned and does not automatically inherit changes to the official loop.
