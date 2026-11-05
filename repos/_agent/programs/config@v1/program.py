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

import contextlib
import hashlib
import json
import math
import struct

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


# Complete, explicit model intent (docs/chatgpt-local-oauth.md). Ordinary raw
# set/get and the optional judge tier retain their existing behavior.

_set = type({""})

_MODEL_TARGET = "llm.configuration.target"
_MODEL_ABANDONED = "llm.configuration.abandoned"
_MODEL_TIERS = ("codegen", "classify", "vision")
_MODEL_SEARCH = ("websearch", "deepresearch")
_MODEL_KEYS = ((_MODEL_TARGET, _MODEL_ABANDONED)
               + tuple("llm.tier." + t for t in _MODEL_TIERS)
               + tuple("search.provider." + s for s in _MODEL_SEARCH))


def _canonical(value, depth=0):
    if depth > 32:
        raise ValueError("invalid_model_configuration")
    if value is None:
        result = "n"
    elif isinstance(value, bool):
        result = "t" if value else "f"
    elif isinstance(value, (int, float)):
        n = float(value)
        if not math.isfinite(n) or abs(n) > 9007199254740991:
            raise ValueError("invalid_model_configuration")
        result = "d" + struct.pack(">d", n if n else 0.0).hex()
    elif isinstance(value, str):
        b = value.encode("utf-8")
        result = "s" + str(len(b)) + ":" + b.hex()
    elif isinstance(value, (tuple, type([]))):
        result = ("a" + str(len(value)) + "["
                  + "".join(_canonical(v, depth + 1) for v in value) + "]")
    elif isinstance(value, dict) and all(isinstance(k, str) for k in value):
        result = "o" + str(len(value)) + "{" + "".join(
            _canonical(k, depth + 1) + _canonical(value[k], depth + 1)
            for k in sorted(value, key=lambda k: k.encode("utf-8"))) + "}"
    else:
        raise ValueError("invalid_model_configuration")
    if len(result) + len("bao-model-v1\n") > 131072:
        raise ValueError("invalid_model_configuration")
    return result


def _parse_target(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 131072:
        raise ValueError("invalid_model_target")
    target = json.loads(text)
    if (not isinstance(target, dict) or target.get("schemaVersion") != 1
            or isinstance(target.get("schemaVersion"), bool)
            or not {"schemaVersion", "applyId", "tiers"} <= target.keys()
            or target.keys() - {"schemaVersion", "applyId", "tiers", "search"}
            or not isinstance(target.get("applyId"), str)
            or not target["applyId"] or len(target["applyId"].encode("utf-8")) > 256
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in target["applyId"])
            or not isinstance(target.get("tiers"), dict)
            or target["tiers"].keys() != _set(_MODEL_TIERS)
            or not all(isinstance(v, dict) for v in target["tiers"].values())):
        raise ValueError("invalid_model_target")
    search = target.get("search", {})
    if (not isinstance(search, dict) or search.keys() - _set(_MODEL_SEARCH)
            or not all(isinstance(v, dict) or v is None for v in search.values())):
        raise ValueError("invalid_model_target")
    _canonical(_target_values(target))
    return target


def _target_values(target):
    return ({"tiers": target["tiers"], "search": target["search"]}
            if target.get("search") else target["tiers"])


def _model_rows(space):
    any_ = use("agent:any@v1")  # noqa: F821
    obj = any_.bundle_child(space, _BUNDLE, _CONFIG_SEED)["objectId"]
    rows = any_.query(space, obj, _DATASET, limit=1025,
                      filter={"$or": [{"id": {"$in": _MODEL_KEYS}}, {"key": {"$in": _MODEL_KEYS}}]})
    if len(rows) >= 1025:
        raise ValueError("ambiguous_model_identity")
    values, seen = {}, _set()
    for row in rows:
        rid, key = row.get("id"), row.get("key")
        if rid not in _MODEL_KEYS and key not in _MODEL_KEYS:
            continue
        if rid != key or key in seen:
            raise ValueError("ambiguous_model_identity")
        seen.add(key)
        if "value" in row:
            values[key] = row["value"]
    return any_, obj, values


def _abandoned_ids(rows):
    values = rows.get(_MODEL_ABANDONED, [])
    if (not isinstance(values, type([])) or len(values) > 1024
            or any(not isinstance(v, str) or len(v) != 64
                   or any(c not in "0123456789abcdef" for c in v) for v in values)
            or len(_set(values)) != len(values)):
        raise ValueError("invalid_model_cancellation")
    return _set(values)


def _apply_hash(apply_id):
    return hashlib.sha256(apply_id.encode("utf-8")).hexdigest()


def _saved_target(rows, apply_id):
    if _MODEL_TARGET not in rows:
        raise ValueError("configuration_incomplete")
    current = _parse_target(rows[_MODEL_TARGET])
    if current["applyId"] != apply_id:
        raise ValueError("configuration_superseded")
    return current


def _verify_models(rows, target):
    current = _saved_target(rows, target["applyId"])
    if _canonical(current) != _canonical(target):
        raise ValueError("configuration_superseded")
    if _apply_hash(target["applyId"]) in _abandoned_ids(rows):
        raise ValueError("configuration_abandoned")
    wanted = {"llm.tier." + t: target["tiers"][t] for t in _MODEL_TIERS}
    wanted.update({"search.provider." + s: v for s, v in target.get("search", {}).items()})
    if any(key not in rows or _canonical(rows[key]) != _canonical(value)
           for key, value in wanted.items()):
        raise ValueError("configuration_incomplete")
    identity = "bao-model-v1\n" + _canonical(_target_values(target))
    return {"configuration": "matched", "applyId": target["applyId"],
            "configId": "v1:" + hashlib.sha256(identity.encode()).hexdigest()}


@span(kind="mutator")  # noqa: F821
def apply_models(spaceConfig, tiers, search=None):
    """Apply explicit codegen/classify/vision intent, plus optional search rows.

    One fresh ID, one complete batch, then readback. Never resend this batch
    after an ambiguous outcome. retry_models only verifies that ID; a rejected
    or incomplete apply needs a new explicit Apply with a new ID. Other config,
    including the independent judge tier, is preserved. Matched is not local auth.
    """
    target = {"schemaVersion": 1, "applyId": effect("uuid4", {})["hex"],  # noqa: F821
              "tiers": tiers}
    if search:
        target["search"] = search
    text = json.dumps(target, ensure_ascii=False, allow_nan=False)
    target = _parse_target(text)
    any_, obj, rows = _model_rows(spaceConfig)
    try:
        old_id = _parse_target(rows.get(_MODEL_TARGET))["applyId"]
    except (ValueError, TypeError, UnicodeError):
        old_id = None
    if old_id == target["applyId"]:
        raise ValueError("fresh_apply_id_required")
    wanted = {_MODEL_TARGET: text, _MODEL_ABANDONED: []}
    wanted.update({"llm.tier." + t: target["tiers"][t] for t in _MODEL_TIERS})
    wanted.update({"search.provider." + s: v for s, v in target.get("search", {}).items()})
    records = [{"id": key, "upsert": True, "ops": [
        {"type": "$set", "path": "key", "value": key},
        {"type": "$set", "path": "value", "value": value}]} for key, value in wanted.items()]
    try:
        # Any applies a synced modify batch as one DAG change / WriteTx. A
        # rejection can still omit an op; only complete readback proves success.
        result = any_.modify(spaceConfig, {"objectId": obj, "dataset": _DATASET,
                                          "records": records})
        if result.get("rejections"):
            raise ValueError("configuration_incomplete")
    except Exception:
        pass  # Unknown/rejected delivery is checked, never replayed.
    _, _, rows = _model_rows(spaceConfig)
    return _verify_models(rows, target)


@span(kind="mutator")  # noqa: F821
def retry_models(spaceConfig, apply_id):
    """Read-only delivery verification for this expected apply; never resends it."""
    _, _, rows = _model_rows(spaceConfig)
    return _verify_models(rows, _saved_target(rows, apply_id))


@span(kind="mutator")  # noqa: F821
def abandon_models(spaceConfig, apply_id):
    """Abandon only this apply's readiness claim, without rolling back model rows.

    An additive, apply-scoped marker cannot erase a newer target. Fresh Apply
    resets old markers atomically with its new ID and complete values.
    """
    any_, obj, rows = _model_rows(spaceConfig)
    _saved_target(rows, apply_id)
    abandoned = _abandoned_ids(rows)
    identity = _apply_hash(apply_id)
    if identity not in abandoned:
        if len(abandoned) >= 1024:
            raise ValueError("invalid_model_cancellation")
        # Read back the scoped marker after a lost/rejected reply.
        with contextlib.suppress(Exception):
            any_.modify(spaceConfig, {"objectId": obj, "dataset": _DATASET, "records": [{
                "id": _MODEL_ABANDONED, "upsert": True, "ops": [
                    {"type": "$set", "path": "key", "value": _MODEL_ABANDONED},
                    {"type": "$addToSet", "path": "value", "value": identity}]}]})
    _, _, rows = _model_rows(spaceConfig)
    _saved_target(rows, apply_id)
    if identity not in _abandoned_ids(rows):
        raise ValueError("configuration_incomplete")
    return {"configuration": "abandoned", "applyId": apply_id}


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
