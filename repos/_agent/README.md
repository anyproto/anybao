# _agent — the bao agent itself: conversation loop, tools, skills

The agent code overlay (ADR-009 §2): every space-resident unit the
runtime loads — the toolcaller loop, the tool programs (`any@v1`,
`memory@v1`, `recall@v1`, `webSearch@v1`, …), the cron programs
(extraction, rollup, decay, …), and the `_`-prefixed system skills
that compose the agent's prompt. Published with `anyrt deploy
--source repos/_agent --target <space|overlay>`; consumers join it
read-only and import via the `agent:` alias.
