"""Author and manage Mini Apps — embeddable HTML/JS apps in the space.

One app = one `mini_app` object addressed by NAME (lowercase, no
spaces, e.g. "coin-flipper"); `source` (full HTML), persisted `state`
(JSON), and `readme` live as separate fields, so state updates never
rewrite source. Prefer `edit()` for small changes and
`get_source(..., frm=, to=)` for big reads. Authoring contract:
`help(ma.create)`."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# ADR-008 §6. `mini_app` is a harness-declared USER type (xKey
# `mini_app`) this program — its writer — ensures per space (ADR-017
# §1); content lives in its runtime dataset `mini_app` (declared
# without a search mapping: HTML is code, never indexed), single record
# "main", flat string fields; writes are per-field $set ops. Every
# source write runs the runtime-script guard: author
# copies of the react/react-dom/useAnytypeState tags are stripped and
# all three are prepended in load order (they must precede the
# author's inline script).

import json
import re

_TYPE = "mini_app"
_DATASET = "mini_app"
_RECORD = "main"
_TYPE_DECL = {"name": "Mini App", "xKey": _TYPE}
_DATASET_DECL = {
    "name": _DATASET, "displayName": "Mini App",
    "idRule": "user", "deleteBy": "anyone", "dynamic": True,
    "fields": [{"key": "source", "kind": "string", "mutableBy": "any"},
               {"key": "state", "kind": "string", "mutableBy": "any"},
               {"key": "readme", "kind": "string", "mutableBy": "any"}]}
_ensured = set()

# the embed loads these by relative src; they must precede the
# author's inline <script>, react before react-dom
_RUNTIME_SCRIPTS = ["react.js", "react-dom.js", "useAnytypeState.js"]


def _tag_rx(name):
    """The full (empty-body) runtime script element plus its line."""
    return re.compile(
        r"[ \t]*<script[^>]*src=[\"']\./" + re.escape(name)
        + r"[\"'][^>]*>\s*</script>[ \t]*\n?")


def _client():
    return use("any@v1")  # noqa: F821 - guest global


def _has_store(c, space):
    return any((t.get("xKey") or t.get("key")) == _TYPE
               for t in c.list_types(space))


def _ensure_store(c, space):
    """Idempotently declare the `mini_app` type + dataset (cached per run)."""
    if space in _ensured:
        return
    c.create_type(space, _TYPE_DECL)
    c.create_dataset(space, _TYPE, _DATASET_DECL)
    _ensured.add(space)


def _find(c, space, name):
    if not _has_store(c, space):
        return None   # no mini_app type = no apps here yet
    for row in c.query_objects(space, filter={"any.types": _TYPE}):
        if ((row.get("any") or {}).get("name")) == name:
            return row["id"]
    return None


def _parts(c, space, oid):
    recs = c.query(space, oid, _DATASET)
    rec = next((r for r in recs if r.get("id") == _RECORD),
               recs[0] if recs else {})
    return {"source": rec.get("source") if isinstance(rec.get("source"), str) else "",
            "state": rec.get("state"),
            "readme": rec.get("readme") if isinstance(rec.get("readme"), str) else ""}


def _set_fields(c, space, oid, fields):
    if not fields:
        return
    c.modify(space, {"objectId": oid, "dataset": _DATASET,
                     "records": [{"id": _RECORD, "upsert": True,
                                  "ops": [{"type": "$set", "path": k, "value": v}
                                          for k, v in fields.items()]}]})


def _guard(html):
    """(html, warnings|None): normalize the runtime script tags — strip
    the author's copies, prepend all three in load order. Presence-only
    injection let a wrong-ordered source (react-dom before react) pass
    untouched and break in every embed; only these three tags are ever
    touched."""
    stripped, found = html, []
    for name in _RUNTIME_SCRIPTS:
        stripped, n = _tag_rx(name).subn("", stripped)
        if n:
            found.append(name)
    inject = "".join(f'<script src="./{n}"></script>\n'
                     for n in _RUNTIME_SCRIPTS)
    out = inject + stripped
    if out == html:
        return html, None
    warnings = []
    missing = [n for n in _RUNTIME_SCRIPTS if n not in found]
    if missing:
        warnings.append(
            "auto-injected missing runtime script(s): " + ", ".join(missing))
    if found:
        warnings.append("moved runtime script tag(s) to canonical load "
                        "order: " + ", ".join(found))
    return out, warnings


def _ser_state(state):
    """None passes through (field untouched/cleared by the caller's
    contract); a string is written verbatim; anything else JSONifies."""
    if state is None or isinstance(state, str):
        return {"ok": True, "text": state}
    try:
        return {"ok": True, "text": json.dumps(state, indent=2)}
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"state is not JSON-serializable: {e}"}


def _edit_string(source, old, new, replace_all):
    if not isinstance(old, str) or not old:
        return None, "old_string is required"
    if not isinstance(new, str):
        return None, "new_string must be a string"
    if old == new:
        return None, "old_string and new_string are identical"
    count = source.count(old)
    if count == 0:
        return None, "No match found for old_string"
    if count > 1 and not replace_all:
        return None, (f"Found {count} matches for old_string; provide more "
                      "surrounding context to make it unique (or set "
                      "replace_all=True)")
    return (source.replace(old, new) if replace_all
            else source.replace(old, new, 1)), count


def _slice(text, frm, to):
    """1-indexed inclusive line slice + its range descriptor."""
    lines = text.split("\n")
    total = len(lines)
    f = max(1, frm) if frm is not None else 1
    t = min(total, to) if to is not None else total
    f = min(f, t) if t else f
    return ("\n".join(lines[f - 1:t]),
            {"from": f, "to": t, "totalLines": total})


@span(kind="mutator")  # noqa: F821 - guest global
def create(space, name, source, state=None, readme=""):
    """Create a mini app; returns {ok, id, name, warnings?}.

    Errors (as {ok: False, error}) if the name is taken — names are
    the addressing key; use `update` to change an existing app.

    Writing an app: the iframe preloads `React`, `ReactDOM`, and
    `useAnytypeState(initial)` — the persistent `React.useState` (it
    reads/writes the app's `state` field across reloads). No JSX, no
    build step, no external CDNs: a mount div plus one inline script,
    `var h = React.createElement`, render with
    `ReactDOM.createRoot(document.getElementById("app")).render(h(App))`.
    The required `<script src="./react.js">`-style tags are normalized
    on every source write — your copies are stripped and the three are
    prepended in load order (reported as `warnings`) — so just omit
    them."""
    if not name or not isinstance(name, str):
        return {"ok": False, "error": "name is required"}
    if not source or not isinstance(source, str):
        return {"ok": False, "error": "source is required (full HTML)"}
    c = _client()
    if _find(c, space, name):
        return {"ok": False,
                "error": f"mini app '{name}' already exists. Use update()."}
    html, warnings = _guard(source)
    st = _ser_state(state)
    if not st["ok"]:
        return {"ok": False, "error": st["error"]}
    _ensure_store(c, space)
    oid = c.create_object(space, {
        "types": [_TYPE],
        "initialProperties": {"any": {"name": name}}})["objectId"]
    fields = {"source": html, "readme": readme or ""}
    if st["text"] is not None:
        fields["state"] = st["text"]
    _set_fields(c, space, oid, fields)
    out = {"ok": True, "id": oid, "name": name}
    if warnings:
        out["warnings"] = warnings
    return out


@span(kind="mutator")  # noqa: F821 - guest global
def update(space, name, source=None, state=None, readme=None, title=None):
    """Update any subset of source/state/readme → {ok, id, name, warnings?}.

    None/omitted fields stay untouched (so update cannot CLEAR state —
    that's `set_state(space, name, None)`). `title` renames the
    OBJECT — avoid: name is the addressing key, lookups by the old
    name break."""
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return {"ok": False, "error": f"mini app not found: {name}"}
    fields, warnings = {}, None
    if source is not None:
        fields["source"], warnings = _guard(source)
    if state is not None:
        st = _ser_state(state)
        if not st["ok"]:
            return {"ok": False, "error": st["error"]}
        fields["state"] = st["text"]
    if readme is not None:
        fields["readme"] = readme
    _set_fields(c, space, oid, fields)
    if title:
        c.update_object(space, oid, {"name": title})
    out = {"ok": True, "id": oid, "name": name}
    if warnings:
        out["warnings"] = warnings
    return out


@span(kind="mutator")  # noqa: F821 - guest global
def edit(space, name, old_string, new_string, replace_all=False, block="source"):
    """Surgical string replacement in `source` or `state` — the cheap path.

    For small changes: no full-source round-trip through tokens.

    `old_string` must match exactly once unless `replace_all=True`
    (ambiguity errors tell you the match count). Returns `{ok, id,
    name, block, replacements, length_before, length_after,
    warnings?}`; failures carry `{ok: False, error, length_before?}`."""
    if block not in ("source", "state"):
        return {"ok": False, "error": "block must be 'source' or 'state'"}
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return {"ok": False, "error": f"mini app not found: {name}"}
    parts = _parts(c, space, oid)
    current = parts[block]
    if not current:
        return {"ok": False, "name": name, "block": block,
                "error": f"block '{block}' is empty or missing"}
    new_block, count_or_err = _edit_string(current, old_string, new_string,
                                           replace_all)
    if new_block is None:
        return {"ok": False, "name": name, "block": block,
                "error": count_or_err, "length_before": len(current)}
    warnings = None
    if block == "source":
        new_block, warnings = _guard(new_block)
    _set_fields(c, space, oid, {block: new_block})
    out = {"ok": True, "id": oid, "name": name, "block": block,
           "replacements": count_or_err, "length_before": len(current),
           "length_after": len(new_block)}
    if warnings:
        out["warnings"] = warnings
    return out


@span(kind="getter")  # noqa: F821 - guest global
def get(space, name, frm=None, to=None):
    """The whole app {id, name, source, state, readme}, or None.

    `state` comes back parsed (raw string if unparseable, None if
    absent); frm/to slice the source (1-indexed, inclusive) and add a
    `range: {from, to, totalLines}` descriptor."""
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return None
    parts = _parts(c, space, oid)
    state = None
    if parts["state"] is not None:
        try:
            state = json.loads(parts["state"])
        except ValueError:
            state = parts["state"]
    out = {"id": oid, "name": name, "source": parts["source"],
           "state": state, "readme": parts["readme"]}
    if frm is not None or to is not None:
        out["source"], out["range"] = _slice(parts["source"], frm, to)
    return out


@span(kind="getter")  # noqa: F821 - guest global
def get_source(space, name, frm=None, to=None):
    """Just {name, source}, or None when the app doesn't exist.

    `frm`/`to` (1-indexed, inclusive) slice the source and add a
    `range` descriptor — read a big app in windows instead of whole."""
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return None
    src = _parts(c, space, oid)["source"]
    if frm is None and to is None:
        return {"name": name, "source": src}
    sliced, rng = _slice(src, frm, to)
    return {"name": name, "source": sliced, "range": rng}


@span(kind="getter")  # noqa: F821 - guest global
def list(space):  # noqa: A001 - the tool surface name (ADR-008 §6)
    """Every mini app in the space: [{id, name}], name-sorted."""
    c = _client()
    if not _has_store(c, space):
        return []
    apps = [{"id": row["id"], "name": (row.get("any") or {}).get("name")}
            for row in c.query_objects(space, filter={"any.types": _TYPE})]
    return sorted([a for a in apps if a["name"]], key=lambda a: a["name"])


@span(kind="mutator")  # noqa: F821 - guest global
def set_state(space, name, state):
    """Overwrite the persisted state (what useAnytypeState reads).
    `None` clears it; anything else is JSONified."""
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return {"ok": False, "error": f"mini app not found: {name}"}
    st = _ser_state(state)
    if not st["ok"]:
        return {"ok": False, "error": st["error"]}
    _set_fields(c, space, oid, {"state": st["text"]})
    return {"ok": True, "id": oid, "name": name}


@span(kind="getter")  # noqa: F821 - guest global
def get_state(space, name):
    """The parsed state object, or None.

    None when the app is missing, state is empty, or the stored text
    isn't valid JSON."""
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return None
    raw = _parts(c, space, oid)["state"]
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@span(kind="mutator")  # noqa: F821 - guest global
def upsert_readme(space, name, readme):
    """Set the readme markdown ("" clears)."""
    if not isinstance(readme, str):
        return {"ok": False, "error": "readme must be a string ('' clears)"}
    c = _client()
    oid = _find(c, space, name)
    if not oid:
        return {"ok": False, "error": f"mini app not found: {name}"}
    _set_fields(c, space, oid, {"readme": readme})
    return {"ok": True, "id": oid, "name": name}


def main(args):
    if args and args.get("space") and args.get("name"):
        return get(args["space"], args["name"]) or f"mini app not found: {args['name']}"
    if args and args.get("space"):
        return list(args["space"])
    return {"ok": False, "error": "space arg required"}
