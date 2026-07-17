### research(space, question, opts?) [mutator]
Research `question` and write the result pages into `space`. `opts`:
`{"chatId": …}` posts progress bubbles to that chat while running
(with `"agentName"`, default "bao"); omit for a silent run. Phases:
grounded Gemini call → follow-up decomposition (llm classify tier,
3-7 questions; an unparseable reply degrades to a single research
page) → batched grounded follow-up calls → write-out. Page type:
existing `pages`/`page` xKey in the space, else an idempotent
`create_type("Page")`. Returns `{ok: True, overviewPageId,
overviewPageName, subPages: [{id, name}], answer, sources: [{url,
title, domain}], searchQueries, timing: {phase1Ms, phase2Ms, phase3Ms,
totalMs}, usage: {totalTokens}}`; provider/config failures return
`{ok: False, error}` instead of raising. Provider/model come from the
`search.provider.deepresearch` config key; the api key is
host-injected (never visible here).
