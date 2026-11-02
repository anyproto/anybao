"""Agent config store: the model behind each LLM tier and search tool.

Dotted keys (`llm.tier.*`, `search.provider.*`, or a program's own
`myProgram.threshold`), rows in the home space, synced to every
device, live for the next cell — never a restart or a file edit.
`cfg.list(baoSpaceConfig)`, `cfg.get(key)`, `cfg.set(key, value)`;
`cfg.set_model("search.provider.websearch", "gemini-3.7-flash")` swaps
only the model. Provider values are `{provider, model, base_url,
api_key_ref, timeout?}` (+ `backend`/`profile`/`traits`/`stream`/
`options` on LLM tiers — fields in `set`). API keys are NOT config
(the Credentials flow); no unset — set null."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# The store is the home space's `agent_config` dataset (ADR-006 §3):
# one `{key, value}` row per dotted key, seeded on the first serve of a
# fresh space from the runtime's defaults. `config.get`/`config.set`
# read and write that row through the host; `list` reads the dataset
# itself through any@v1 (the config child of the bao/v1 bundle).
# Provider values are `{provider, model, base_url, api_key_ref,
# timeout?}` (+ `backend`/`profile`/`traits`/`stream`/`options` on LLM
# tiers, ADR-005 §1.1; a search row's `provider` picks its wire, ADR-008 §3).

_BUNDLE = "bao/v1"
_CONFIG_SEED = "bao/config/v1"
_DATASET = "agent_config"
_MCP_ROW = "mcp.server."
_MCP_KEY = "mcp.key."


def _check_mcp_row(key, value):
    """ADR-032 §2/§3: `mcp.server.<id>` = `{transport: "http", url,
    api_key_ref: "mcp.key.<id>" | None}` — the key ref is always in the
    `mcp.key.*` namespace. Refused here, at the write, so a misspelt
    ref never reaches the store (the broker would refuse it later as
    undeclared, after the row looked fine). `None` (unset) passes."""
    if value is None:
        return
    sid = key[len(_MCP_ROW):]
    shape = f"{{transport: 'http', url, api_key_ref: '{_MCP_KEY}{sid}' | None}}"
    if not (sid and sid[0].isalpha() and sid == sid.lower()
            and all(c.isalnum() or c in "_-" for c in sid)):
        raise ValueError(f"{key!r}: an MCP server id is [a-z][a-z0-9_-]* (got {sid!r})")
    if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not value["url"]:
        raise ValueError(f"{key!r}: an MCP server row is {shape} (got {value!r})")
    ref = value.get("api_key_ref")
    if ref is not None and not (isinstance(ref, str) and ref.startswith(_MCP_KEY)):
        raise ValueError(f"{key!r}: api_key_ref must be `{_MCP_KEY}{sid}` — an MCP key ref is "
                         f"always `mcp.key.<id>`, never `<id>.key.mcp` or `<id>.mcp.key` — "
                         f"or None for a keyless server (got {ref!r})")


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
    api_key_ref, timeout?, backend?, profile?, traits?, stream?,
    options?}` object (the last five on LLM tiers; a search row's
    `provider` is `gemini` or `openai-compat`) — `set_model` changes
    only the model. `api_key_ref: null` = a keyless local server.
    `timeout` is seconds (or `{idle, total}` on an LLM tier).
    An `mcp.server.<id>` row is `{transport: "http", url, api_key_ref:
    "mcp.key.<id>" | None}` — the key ref is always `mcp.key.<id>`;
    any other spelling is refused (ValueError). Refused for secret
    refs."""
    if key.startswith(_MCP_ROW):
        _check_mcp_row(key, value)
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
