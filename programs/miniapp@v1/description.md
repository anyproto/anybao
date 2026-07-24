Author and manage Mini Apps — small embeddable HTML/JS apps the user
opens as objects in their space. One app = one `mini_app` object,
addressed by NAME (lowercase, no spaces, e.g. `"coin-flipper"`); its
`source` (full HTML), persisted `state` (JSON), and `readme` live as
separate fields, so state updates never rewrite source.

Writing an app: the iframe preloads `React`, `ReactDOM`, and
`useAnytypeState(initial)` — the persistent `React.useState` (it
reads/writes the app's `state` field across reloads). No JSX, no build
step, no external CDNs: a mount div plus one inline script,
`var h = React.createElement`, render with
`ReactDOM.createRoot(document.getElementById("app")).render(h(App))`.
The required `<script src="./react.js">`-style tags are normalized on
every source write — your copies are stripped and the three are
prepended in load order (reported as `warnings`) — so just omit them.

```python
ma = use("miniapp@v1")
ma.create(space, "coin-flipper", source_html, state={"flips": 0})
ma.edit(space, "coin-flipper", old_string="Heads", new_string="HEADS")
```

Prefer `edit()` for small changes and `get_source(..., frm=, to=)` for
reading big sources — full-source round-trips burn tokens. Mutators
return `{ok: False, error}` instead of raising; getters return `None`
on a missing app.
