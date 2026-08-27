"""Agent config store — the model behind each LLM tier and search
tool (`llm.tier.*`, `search.provider.*`), plus any dotted key a
program wants to keep (`myProgram.threshold`).

Rows in the home space, synced to every device, live for the next
cell — a change never needs a restart or a file edit. `cfg.list(
baoSpaceConfig)` shows everything; `cfg.get(key)`; `cfg.set(key,
value)`; `cfg.set_model("search.provider.websearch",
"gemini-3.7-flash")` swaps only the model. Provider values are
`{provider, model, base_url, api_key_ref}`. API keys are NOT config
(the Credentials flow); there is no unset — set a value to null."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The store is the home space's `agent_config` dataset (ADR-006 §3):
# one `{key, value}` row per dotted key, seeded on the first serve of a
# fresh space from the runtime's defaults. `config.get`/`config.set`
# read and write that row through the host; `list` reads the dataset
# itself through any@v1 (the config child of the bao/v1 bundle).
# Provider values are `{provider, model, base_url, api_key_ref}`.

_BUNDLE = "bao/v1"
_CONFIG_SEED = "bao/config/v1"
_DATASET = "agent_config"


@span(kind="getter")  # noqa: F821 - guest global
def list(spaceConfig):  # noqa: A001 - the tool surface reads better as list()
    """Every entry as `{key: value}`; pass `baoSpaceConfig`.

    A plain query of the home space's `agent_config` dataset."""
    any_ = use("agent:any@v1")  # noqa: F821
    obj = any_.bundle_child(spaceConfig, _BUNDLE, _CONFIG_SEED)["objectId"]
    rows = any_.query(spaceConfig, obj, _DATASET)
    return {r["key"]: r.get("value") for r in rows if r.get("key")}


@span(kind="getter")  # noqa: F821
def get(key):
    """The value under one dotted key, e.g. `llm.tier.codegen`.

    Raises `ConfigError` for an unknown key or a secret ref."""
    return effect("config.get", {"key": key})["value"]  # noqa: F821


@span(kind="mutator")  # noqa: F821
def set(key, value):  # noqa: A001
    """Persist `value` under `key`; live for the next cell.

    Provider keys take the whole `{provider, model, base_url,
    api_key_ref}` object — `set_model` changes only the model.
    Refused for secret refs."""
    effect("config.set", {"key": key, "value": value})  # noqa: F821
    return {"key": key, "value": value}


@span(kind="mutator")  # noqa: F821
def set_model(key, model):
    """Swap only the `model` of a tier or search-provider entry.

    `key` = `llm.tier.<tier>` or `search.provider.<tool>`; provider,
    base_url and key ref are kept."""
    value = dict(get(key))
    if "model" not in value:
        raise ValueError(f"{key!r} is not a provider entry (no `model` field)")
    value["model"] = model
    return set(key, value)


def main(args):
    args = args or {}
    if "value" in args:
        return set(args["key"], args["value"])
    if "model" in args:
        return set_model(args["key"], args["model"])
    if args.get("key"):
        return get(args["key"])
    if args.get("space"):
        return list(args["space"])
    raise ValueError("args: {key} | {key, value} | {key, model} | {space: baoSpaceConfig}")
