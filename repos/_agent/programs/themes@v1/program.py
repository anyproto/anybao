"""Switch and preview Any themes with Keep/Revert in one chosen UI window.

Call discover(), choose a windowId, then request(action, window_id, params).
The frontend owns validation and saved preferences; Color mode is separate.
Preview is temporary until Keep/commit. Claim success only from ok: True;
outcome_unknown is not a failed write and must not be automatically retried.
For prompt/image creation, read any.get_skill("themes") first.
"""

__any_tool__ = True


@span("themes.discover", kind="getter")  # noqa: F821 - guest global
def discover(timeout=1):
    """Find theme-capable UI sessions → {windows: [UI replies]} or {ok: False, error}.

    Each successful reply has windowId, result.active/focused/actions/limits
    and state. Choose one unambiguous session; otherwise ask the user.
    No target is saved. A UI reload requires discovery again. timeout is
    seconds in (0, 30]; collection ends at the absolute deadline.
    """
    return effect("ui.theme", {"action": "capabilities", "timeout": timeout})  # noqa: F821


@span("themes.request", kind="mutator")  # noqa: F821 - guest global
def request(action, window_id, params=None, timeout=5):
    """Send one theme action → the UI's {ok, requestId, windowId, state, result/error}.

    Use the discovered windowId; params keeps the frontend's camelCase keys:
    list/catalogue/get (get optionally {themeId}); select {themeId};
    preview {themeId} or {file: {spaceId, fileId}, replace?}; install uses file;
    commit/cancel {previewId}; options.set {themeId, settingId, value};
    options.reset {themeId}; appearance.set {mode: system/light/dark};
    export {themeId, spaceId, objectId}. get includes declared settings/overrides.
    Preview returns result.previewId and state.preview; saved selection stays
    unchanged until Keep/commit. Prefer preview for newly created looks.
    Transport failure: {ok: False, requestId, error: {code, message}}.
    outcome_unknown needs a fresh get/discovery, never an automatic write retry.
    timeout is seconds in (0, 30]. No UI or unsupported builds cannot apply.
    """
    return effect("ui.theme", {  # noqa: F821
        "action": action, "windowId": window_id,
        "params": {} if params is None else params, "timeout": timeout,
    })
