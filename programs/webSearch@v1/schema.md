### search(*queries) [getter]
One or more query strings (variadic; a single list argument is also
accepted). Returns a list of formatted strings, one per query, in
order: `[N] <query>` + the primary source url + the synthesized answer
+ a `Sources:` list of the remaining grounding sources. A query that
fails yields `[ERROR] query N ("…") failed: <reason>` in its slot —
the call itself never raises for a provider-side failure. Empty input
returns `[]`. Provider/model come from the `search.provider.websearch`
config key; the api key is host-injected (never visible here).
