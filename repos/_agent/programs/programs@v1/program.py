"""Write, edit and delete programs in a working space — live on the next use().

The agent-side program write path (ADR-013): a saved program is
bit-identical in shape to what deploy writes (source in
program_source/"main", docstring-derived summary, `name@vN`), so
list_programs / use() / help() need no second surface. Write-time
validation mirrors deploy's scan; a passing post-save use() probe
makes a `__any_tool__` program a live tool immediately, and an edit
is live on the very next use(). Overlay-exported specs are refused
(a working-space copy would shadow the pipeline's). Promotion to an
overlay repo stays a human deploy step (ADR-013 §6)."""

__any_tool__ = True  # agent-callable (ADR-010 §4)

# Validation is deploy-parity, guest-implemented (ADR-013 §3): the
# line-scan helpers below are deploy.rs ports (the Rust scan stays
# normative — the shared corpus tests/fixtures/program_validation.jsonl
# breaks on drift), the ast checks are the write path's stricter
# guest-only layer. All writes go through any@v1 — no new host effects.

import ast

_SUMMARY_MAX_CHARS = 80      # ADR-010 §1 hard caps
_DOCSTRING_MAX_LINES = 12
_DOCSTRING_MAX_CHARS = 800

_a = None


def _any():
    global _a
    if _a is None:
        _a = use("any@v1")  # noqa: F821 - guest global
    return _a


# --- static source scan (deploy.rs ports — keep line-for-line parity) --------

def _module_docstring(code):
    """The source's first statement when it is a string literal
    (shebang/comment/blank lines skipped), quotes stripped — the
    deploy.rs `module_docstring` scan, ported."""
    rest = code
    while True:
        nl = rest.find("\n")
        line_end = nl + 1 if nl != -1 else len(rest)
        line = rest[:line_end].strip()
        if not line or line.startswith("#"):
            if line_end == len(rest):
                return None
            rest = rest[line_end:]
            continue
        break
    stripped = rest.lstrip()
    for q in ('"""', "'''", '"', "'"):
        if stripped.startswith(q):
            body = stripped[len(q):]
            end = body.find(q)
            if end == -1:
                return None
            return body[:end]
    return None


def _summary_of(doc):
    for ln in doc.splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _has_tool_marker(code):
    return any(ln.lstrip().startswith("__any_tool__ = True")
               for ln in code.splitlines())


def _has_public_span_def(code):
    """≥1 `@span(...)`-decorated public def, any nesting — the
    deploy.rs `has_public_span_def` pending-state scan, ported."""
    pending = False
    for line in code.splitlines():
        t = line.lstrip()
        if t.startswith("@span("):
            pending = True
        elif t.startswith("@") or t.startswith("#") or not t:
            pass  # another decorator / comment / blank keeps the pending span
        elif t.startswith("def "):
            if pending and not t[4:].startswith("_"):
                return True
            pending = False
        else:
            pending = False
    return False


# --- write-time validation (ADR-013 §3) --------------------------------------

def _check_name_version(name, version):
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(
            f"program name {name!r} must be a valid identifier "
            '("mailWatch", not "mail-watch" or prose)')
    v = version if isinstance(version, str) else ""
    if not (v.startswith("v") and v[1:].isdigit()):
        raise ValueError(f'program version {version!r} must be "v<N>" (v1, v2, …)')


def _parse_spec(spec):
    if not isinstance(spec, str) or "@" not in spec:
        raise ValueError(f'bad program spec {spec!r} — need "name@vN"')
    name, _, version = spec.rpartition("@")
    _check_name_version(name, version)
    return name, version


def _check_imports(spec, tree):
    # judged by the kernel's own __import__ — the allowlist can't drift
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError(
                    f"{spec}: relative import (line {node.lineno}) — guest "
                    "programs are single modules, there is no package to be "
                    "relative to")
            names = [node.module or ""]
        else:
            continue
        for n in names:
            try:
                __import__(n.split(".")[0])
            except ImportError as e:
                raise ValueError(
                    f"{spec}: import {n!r} (line {node.lineno}) — {e}"
                ) from None


def _check_tool_shape(spec, code, tree):
    if not _has_public_span_def(code):
        raise ValueError(
            f"{spec} declares __any_tool__ but no public @span-tagged def — "
            "span every tool method (ADR-010 §1)")
    # stricter than deploy: EVERY module-level public def (the ADR-010 §3
    # inventory surface) is spanned and documented
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name.startswith("_") or node.name == "main":
            continue
        spanned = any(
            isinstance(d, ast.Call) and getattr(d.func, "id", None) == "span"
            for d in node.decorator_list)
        if not spanned:
            raise ValueError(
                f"{spec}: public def {node.name}() lacks @span(...) — every "
                "tool method is spanned, or _-prefix it to hide it "
                "(ADR-010 §1)")
        if not ast.get_docstring(node):
            raise ValueError(
                f"{spec}: public def {node.name}() has no docstring — first "
                "line = the summary, body = the return shape (ADR-010 §1)")


def _validate_source(spec, code):
    """The pre-write gate: deploy's scan + the guest-only checks
    (ADR-013 §3). Returns the derived summary; raises ValueError
    naming the fix — nothing is written on a refusal."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"{spec}: source must be non-empty program text")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(
            f"{spec}: SyntaxError: {e} — a syntax error is never saved"
        ) from None
    doc = _module_docstring(code)
    if doc is None:
        raise ValueError(
            f"{spec}: no module docstring — write a short one; its first "
            "line is the summary every listing shows (ADR-010 §1)")
    summary = _summary_of(doc)
    if not summary:
        raise ValueError(f"{spec}: module docstring has no content (ADR-010 §1)")
    if len(summary) > _SUMMARY_MAX_CHARS:
        raise ValueError(
            f"{spec}: docstring first line is {len(summary)} chars — the "
            f"one-liner caps at {_SUMMARY_MAX_CHARS} (ADR-010 §1)")
    lines = len(doc.strip().splitlines())
    if lines > _DOCSTRING_MAX_LINES or len(doc) > _DOCSTRING_MAX_CHARS:
        raise ValueError(
            f"{spec}: module docstring is {lines} lines / {len(doc)} chars — "
            f"it is standing prompt, cap {_DOCSTRING_MAX_LINES} lines / "
            f"{_DOCSTRING_MAX_CHARS} chars (ADR-010 §1; move depth into "
            "method docstrings)")
    _check_imports(spec, tree)
    if _has_tool_marker(code):
        _check_tool_shape(spec, code, tree)
    return summary


# --- storage (deploy's exact shape, ADR-013 §1) ------------------------------

def _space_id(spaceConfig):
    return _any().get_space(spaceConfig)["id"]


def _find(sid, name, version):
    rows = _any().query_objects(
        sid, filter={"program.name": name, "program.version": version},
        limit=1)
    return rows[0]["id"] if rows else None


def _overlay_aliases():
    try:
        out = effect("config.get", {"key": "overlays.aliases"})  # noqa: F821
    except EffectError:  # noqa: F821 - guest global
        return {}   # no overlays configured (plain local runs)
    return out.get("value") or {}


def _shadow_guard(sid, name, version):
    """Refuse a spec any joined overlay exports (ADR-013 §1): a
    working-space copy would shadow the pipeline-owned program."""
    for alias, overlay_space in sorted(_overlay_aliases().items()):
        if overlay_space == sid:
            continue  # degenerate alias bound to the working space itself
        if _any().query_objects(
                overlay_space,
                filter={"program.name": name, "program.version": version},
                limit=1):
            raise ValueError(
                f"{name}@{version} is exported by the `{alias}` overlay — a "
                "working-space copy would SHADOW it (ADR-013 §1). Pick a "
                "different name; overlay programs change only through deploy.")


def _probe(sid, spec):
    """Post-save import probe (ADR-013 §3): use() the saved spec,
    space-qualified so it resolves in the target space regardless of
    this module's defining space. None = ok, else the failure text."""
    try:
        use(f"{sid}:{spec}")  # noqa: F821 - guest global
        return None
    except Exception as e:  # noqa: BLE001 — any load failure IS the probe result
        return f"{type(e).__name__}: {e}"


def _write_and_probe(sid, oid, spec, code, summary):
    a = _any()
    a.upsert_record(sid, oid, "program_source", "main", {"code": code})
    probe_err = _probe(sid, spec)
    any_tool = _has_tool_marker(code) and probe_err is None
    a.update_object(sid, oid, {"program": {"any_tool": any_tool,
                                           "summary": summary}})
    out = {"ok": probe_err is None, "objectId": oid, "spec": spec,
           "anyTool": any_tool, "probe": "ok" if probe_err is None else probe_err}
    if probe_err is not None:
        # a broken save is recoverable state, never a half-bound tool
        out["saved"] = True
        out["hint"] = (f'the source IS saved (any_tool stays false) — fix it '
                       f'via edit_program(space, "{spec}", edits)')
    return out


def _has_main(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.FunctionDef) and n.name == "main"
               for n in tree.body)


def _apply_edits(spec, code, edits):
    if not isinstance(edits, list) or not edits:
        raise ValueError(
            'edits must be a non-empty list of {"oldText", "newText", '
            '"replaceAll"?}')
    out = code
    for i, e in enumerate(edits):
        if not isinstance(e, dict) or "oldText" not in e or "newText" not in e:
            raise ValueError(
                f'edit [{i}] must be {{"oldText", "newText", "replaceAll"?}}')
        old = e["oldText"]
        n = out.count(old)
        if n == 0:
            raise ValueError(
                f"edit [{i}]: oldText not found in {spec} — nothing was "
                "applied (all-or-nothing); re-read the source and retry")
        if n > 1 and not e.get("replaceAll"):
            raise ValueError(
                f"edit [{i}]: oldText matches {n} places in {spec} — add "
                "surrounding lines to disambiguate, or set replaceAll")
        out = out.replace(old, e["newText"]) if e.get("replaceAll") \
            else out.replace(old, e["newText"], 1)
    return out


# --- the tool surface (ADR-013 §2) -------------------------------------------

@span("programs.create", kind="mutator")  # noqa: F821 - guest global
def create_program(spaceConfig, body):
    """Create a program in the space → {ok, objectId, spec, anyTool, probe, hint?}.

    `body`: {"name", "source", "version"?} (version defaults "v1";
    accepted keys exactly these). Source rules are the deployed ones
    (ADR-010 §1): short module docstring (first line ≤ 80 chars = the
    listed summary); a tool adds `__any_tool__ = True` +
    `@span("<name>.<method>", kind=...)` and a docstring on every
    public def. Refused loudly (nothing written): syntax errors,
    kernel-forbidden imports, an existing spec, a spec an overlay
    exports. After writing, the source is use()-probed: on success the
    program is live (a tool joins the inventory next turn); on failure
    you get {ok: false, saved: true, hint} — fix via edit_program."""
    body = dict(body or {})
    unknown = set(body) - {"name", "version", "source"}
    if unknown:
        raise ValueError(
            f"create_program: unknown key(s) {sorted(unknown)} — body takes "
            "name / source / version?")
    name = body.get("name")
    version = body.get("version") or "v1"
    _check_name_version(name, version)
    spec = f"{name}@{version}"
    summary = _validate_source(spec, body.get("source"))
    sid = _space_id(spaceConfig)
    if _find(sid, name, version):
        raise ValueError(
            f"{spec} already exists in this space — update_program / "
            "edit_program to change it, or bump the version")
    _shadow_guard(sid, name, version)
    oid = _any().create_object(sid, {
        "types": ["program"],
        "name": name,
        "initialProperties": {"program": {"name": name, "version": version,
                                          "any_tool": False,
                                          "summary": summary}},
    })["objectId"]
    return _write_and_probe(sid, oid, spec, body["source"], summary)


@span("programs.update", kind="mutator")  # noqa: F821 - guest global
def update_program(spaceConfig, spec, source):
    """Replace a program's whole source → {ok, objectId, spec, anyTool, probe, hint?}.

    Full-source replace of an EXISTING working-space program (same
    validation and post-save probe as create_program; the edit is live
    on the next use() — ADR-004 §4). Prefer edit_program for point
    changes; use this when intentionally restructuring."""
    name, version = _parse_spec(spec)
    summary = _validate_source(spec, source)
    sid = _space_id(spaceConfig)
    oid = _find(sid, name, version)
    if not oid:
        raise ValueError(f"{spec} not found in this space — create_program to add it")
    _shadow_guard(sid, name, version)
    return _write_and_probe(sid, oid, spec, source, summary)


@span("programs.edit", kind="mutator")  # noqa: F821 - guest global
def edit_program(spaceConfig, spec, edits):
    """Surgical text edits on a program's source → {ok, objectId, spec, anyTool, probe, hint?}.

    `edits`: [{"oldText", "newText", "replaceAll"?}] — matched against
    the current source, all-or-nothing; oldText must be unique unless
    replaceAll (mirrors edit_markdown). Refused: an edit whose result
    drops main() or the module docstring, or fails any create-time
    check — nothing is written on a refusal. The edited program is
    use()-probed like a create; live on the next use()."""
    name, version = _parse_spec(spec)
    sid = _space_id(spaceConfig)
    oid = _find(sid, name, version)
    if not oid:
        raise ValueError(f"{spec} not found in this space")
    recs = _any().query(sid, oid, "program_source")
    if not recs:
        raise ValueError(f"{spec} has no source record")
    old_code = recs[0].get("code") or ""
    code = _apply_edits(spec, old_code, edits)
    summary = _validate_source(spec, code)
    if _has_main(old_code) and not _has_main(code):
        raise ValueError(
            f"{spec}: this edit drops main() — the entry point triggers and "
            "consumers call; rewrite it instead of deleting it")
    return _write_and_probe(sid, oid, spec, code, summary)


@span("programs.delete", kind="mutator")  # noqa: F821 - guest global
def delete_program(spaceConfig, spec):
    """Delete a working-space program object → {ok, objectId, spec}.

    Permanent (no undo); anything still use()-ing the spec errors on
    its next load. Overlay programs are not deletable here — deploy
    owns them."""
    name, version = _parse_spec(spec)
    sid = _space_id(spaceConfig)
    oid = _find(sid, name, version)
    if not oid:
        raise ValueError(f"{spec} not found in this space")
    _any().delete_object(sid, oid)
    return {"ok": True, "objectId": oid, "spec": spec}
