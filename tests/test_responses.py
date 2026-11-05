"""Synthetic Responses codec and real guest-loop coverage; never live."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_llm_module import LLM, T, load

BASE = "https://chatgpt.com/backend-api/codex"
REF = "connector.oauth.openai-codex"
PROV = {
    "provider": "openai-responses",
    "backend": "openai-codex",
    "model": "gpt-synthetic",
    "base_url": BASE,
    "api_key_ref": REF,
}


def message(text="answer", phase="final_answer"):
    return {
        "id": "lookup_" + phase,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call(args='{"code":"1+1"}'):
    return {
        "id": "lookup_function",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_actual",
        "name": "run_cell",
        "arguments": args,
    }


def reasoning():
    return {
        "id": "lookup_reasoning",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "OPAQUE-DO-NOT-DECODE",
    }


def events(output, status="completed", usage=True):
    out = [{"type": "response.created", "response": {"id": "resp_test", "status": "in_progress"}}]
    for i, item in enumerate(output):
        out += [
            {"type": "response.output_item.added", "output_index": i, "item": item},
            {"type": "response.output_item.done", "output_index": i, "item": item},
        ]
    response = {"id": "resp_test", "status": status, "output": output}
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    if usage:
        response["usage"] = {
            "input_tokens": 25,
            "output_tokens": 12,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens_details": {"reasoning_tokens": 7},
        }
    out.append({"type": "response." + status, "response": response})
    return json.loads(json.dumps(out))


def stream(ev):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ev)


def backend_events(output, terminal_output="full", **kwargs):
    """Synthetic backend lifecycle: added skeletons, deltas, field/part/item done.

    Hermes' codex_runtime.py documents missing/null/[] terminal output. These
    fixtures model those variants, not a captured response from a user account.
    """
    basic = events(output, **kwargs)
    out = [basic[0], {"type": "response.in_progress", "response": basic[0]["response"]}]
    for i, item in enumerate(copy.deepcopy(output)):
        added = copy.deepcopy(item)
        if "status" in added:
            added["status"] = "in_progress"
        if item["type"] == "message":
            added["content"] = []
        elif item["type"] == "function_call":
            added["arguments"] = ""
        else:
            added["summary"] = []
            added.pop("encrypted_content", None)
        out.append({"type": "response.output_item.added", "output_index": i, "item": added})
        identity = {"output_index": i, "item_id": item["id"]}

        def field_events(prefix, field, value, location):
            split = len(value) // 2
            out.extend(
                {"type": prefix + ".delta", **location, "delta": delta}
                for delta in (value[:split], value[split:])
            )
            out.append({"type": prefix + ".done", **location, field: value})

        if item["type"] == "message":
            for ci, part in enumerate(item["content"]):
                field = "text" if part["type"] == "output_text" else "refusal"
                location = {**identity, "content_index": ci}
                out.append({"type": "response.content_part.added", **location,
                            "part": {**part, field: ""}})
                field_events("response." + part["type"], field, part[field], location)
                out.append({"type": "response.content_part.done", **location, "part": part})
        elif item["type"] == "function_call":
            field_events("response.function_call_arguments", "arguments", item["arguments"],
                         identity)
        else:
            for si, part in enumerate(item.get("summary", [])):
                location = {**identity, "summary_index": si}
                out.append({"type": "response.reasoning_summary_part.added", **location,
                            "part": {**part, "text": ""}})
                field_events("response.reasoning_summary_text", "text", part["text"], location)
                out.append({"type": "response.reasoning_summary_part.done", **location,
                            "part": part})
        out.append({"type": "response.output_item.done", "output_index": i, "item": item})
    terminal = basic[-1]
    if terminal_output == "missing":
        terminal["response"].pop("output")
    elif terminal_output != "full":
        terminal["response"]["output"] = None if terminal_output == "null" else []
    out.append(terminal)
    return json.loads(json.dumps(out))


def adapter():
    return LLM["OpenAIResponsesAdapter"]("gpt-synthetic")


def parse(output, **kw):
    a = adapter()
    return a.parse_response(a.parse_stream(stream(events(output, **kw))))


@pytest.mark.parametrize("terminal_output", ["full", "missing", "null", "empty"])
@pytest.mark.parametrize("shape", ["text", "refusal", "function", "mixed", "empty"])
def test_backend_lifecycle_completed_items_are_output_authority(terminal_output, shape):
    refusal = {**message(), "content": [{"type": "refusal", "refusal": "Cannot help."}]}
    thought = {**reasoning(), "summary": [{"type": "summary_text", "text": "Synthetic summary"}]}
    output = {
        "text": [message()], "refusal": [refusal], "function": [call()],
        "mixed": [thought, message("working", "commentary"), call(), message()], "empty": [],
    }[shape]
    ev = backend_events(output, terminal_output)
    for seq, event in enumerate(ev):
        event["sequence_number"] = seq
    a = adapter()
    raw = a.parse_stream(stream(ev))
    assert raw["output"] == output
    assert a.parse_response(raw) == parse(output)


def test_shared_rust_sanitizer_fixture_matches_python_backend_lifecycle():
    thought = {**reasoning(), "summary": [{"type": "summary_text", "text": "Synthetic summary"}]}
    expected = backend_events([thought, message("working", "commentary"), call(), message()])
    for seq, event in enumerate(expected):
        event["sequence_number"] = seq
    fixture = Path(__file__).parent / "fixtures/responses-codex-lifecycle.json"
    assert json.loads(fixture.read_text()) == expected


def invalid_backend_events(mutation, terminal_output="missing"):
    ev = backend_events([message("working", "commentary"),
                         call('{"code":"unsafe_execution = True"}')], terminal_output)

    def event(kind, index=None):
        return next(e for e in ev if e["type"] == "response." + kind
                    and (index is None or e.get("output_index") == index))

    def remove(kind, index=None):
        ev.remove(event(kind, index))

    if mutation == "missing_creation":
        ev = [{"type": "response.completed", "response": {"id": None,
                                                         "status": "completed"}}]
    elif mutation in ("missing_terminal", "missing_item_done", "missing_field_done",
                    "missing_content_done"):
        kind, index = {
            "missing_terminal": ("completed", None),
            "missing_item_done": ("output_item.done", 1),
            "missing_field_done": ("function_call_arguments.done", 1),
            "missing_content_done": ("content_part.done", 0),
        }[mutation]
        remove(kind, index)
    elif mutation == "only_announced_call":
        ev = [e for e in ev if e.get("output_index") != 1
              or e["type"] == "response.output_item.added"]
    elif mutation == "only_terminal_call":
        ev = [e for e in ev if e.get("output_index") != 1]
        ev[-1]["response"]["output"] = [message("working", "commentary"), call()]
    elif mutation == "terminal_identity":
        ev[-1]["response"]["id"] = "foreign"
    elif mutation == "terminal_status":
        ev[-1]["response"]["status"] = "in_progress"
    elif mutation == "wrong_output_type":
        ev[-1]["response"]["output"] = "provider-derived-secret-sentinel"
    elif mutation in ("terminal_conflict", "terminal_partial", "terminal_reordered"):
        output = [message("working", "commentary"), call()]
        if mutation == "terminal_conflict":
            output[1]["arguments"] = '{"code":"different()"}'
        elif mutation == "terminal_partial":
            output.pop()
        else:
            output.reverse()
        ev[-1]["response"]["output"] = output
    elif mutation == "delta_conflict":
        event("function_call_arguments.delta")["delta"] += "different"
    elif mutation == "content_conflict":
        event("content_part.done")["part"]["text"] = "different"
    elif mutation == "item_identity":
        event("output_item.done", 1)["item"]["call_id"] = "different"
    elif mutation == "delta_identity":
        event("function_call_arguments.delta")["item_id"] = "different"
    elif mutation == "unfinished_item":
        event("output_item.done", 1)["item"]["status"] = "in_progress"
    elif mutation == "duplicate_item":
        ev.insert(-1, copy.deepcopy(event("output_item.done", 1)))
    elif mutation == "delta_after_done":
        ev.insert(-1, copy.deepcopy(event("function_call_arguments.delta")))
    elif mutation == "duplicate_terminal":
        ev.append(copy.deepcopy(ev[-1]))
    elif mutation == "item_order":
        event("output_item.added", 1)["output_index"] = 4
    elif mutation == "sequence":
        for e in ev:
            e["sequence_number"] = 0
    elif mutation == "invalid_arguments":
        ev = backend_events([call('{"code":')], terminal_output)
    else:
        raise AssertionError(mutation)
    return ev


INVALID_BACKEND_MUTATIONS = [
    "missing_creation", "missing_terminal", "missing_item_done", "missing_field_done",
    "missing_content_done",
    "only_announced_call", "only_terminal_call", "terminal_identity", "terminal_status",
    "wrong_output_type", "terminal_conflict", "terminal_partial", "terminal_reordered",
    "delta_conflict", "content_conflict", "item_identity", "delta_identity", "unfinished_item",
    "duplicate_item", "delta_after_done", "duplicate_terminal", "item_order", "sequence",
    "invalid_arguments",
]


@pytest.mark.parametrize("terminal_output", ["missing", "null", "empty"])
@pytest.mark.parametrize("mutation", INVALID_BACKEND_MUTATIONS)
def test_backend_omission_does_not_repair_invalid_streams(mutation, terminal_output):
    with pytest.raises(LLM["IncompleteReply"]):
        adapter().parse_stream(stream(invalid_backend_events(mutation, terminal_output)))


@pytest.mark.parametrize("mutation", INVALID_BACKEND_MUTATIONS)
def test_real_kernel_invalid_backend_stream_cannot_execute(mutation):
    from kernelenv import load_kernel
    from test_toolcaller import World

    world = World([])
    client = world.use("any@v1")
    client.chat_log = lambda space, chat: {"objectId": chat}
    client.get_skill = lambda _name: ""
    body = stream(invalid_backend_events(mutation))
    posts = []

    def effect(name, payload):
        if name == "config.get":
            return {"value": PROV}
        if name == "runtime.get":
            return {"value": None}
        if name == "time.now":
            return {"epoch": 1}
        if name == "mailbox.drain":
            return {"items": []}
        if name == "sleep":
            return {}
        assert name == "http.post", (name, payload)
        posts.append(payload)
        return {"status": 200, "body": body, "billingSource": "chatgpt-subscription"}

    kernel = load_kernel(effect=effect, any_client=client)
    result = kernel._run_cell(
        "use('toolcaller@v1').main({'space':'s1','chatId':'c1','userText':'calculate'})", "main"
    )
    assert not result["ok"] and result["error"]["type"] == "IncompleteReply"
    assert len(posts) == 3
    assert "unsafe_execution" not in kernel._ns
    assert not world.chat_posts and not world.turns


def test_stream_diagnostics_only_expose_literal_internal_codes():
    for mutation, code in (("missing_item_done", "unfinished_output_item"),
                           ("wrong_output_type", "invalid_terminal_output"),
                           ("terminal_conflict", "terminal_output_mismatch")):
        with pytest.raises(LLM["IncompleteReply"]) as error:
            adapter().parse_stream(stream(invalid_backend_events(mutation)))
        assert str(error.value) == (
            "llm reply incomplete: invalid or incomplete Responses stream (" + code + ")")
    ev = backend_events([message()], "missing")
    ev[0]["type"] = {"provider-derived-secret-sentinel": "value"}
    with pytest.raises(LLM["IncompleteReply"]) as error:
        adapter().parse_stream(stream(ev))
    assert "provider-derived" not in str(error.value)
    text = 'data: {"type":"response.created","response":"provider-derived-secret-sentinel"}\n\n'
    with pytest.raises(LLM["IncompleteReply"], match=r"\(malformed_event\)$") as error:
        adapter().parse_stream(text)
    assert "provider-derived" not in str(error.value)


@pytest.mark.parametrize("terminal_output", ["missing", "null", "empty"])
@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_omission_never_promotes_unsuccessful_output_to_calls(terminal_output, status):
    ev = backend_events([message("```cell\nunsafe_execution = True\n```"), call()],
                        terminal_output, status=status)
    try:
        raw = adapter().parse_stream(stream(ev))
    except LLM["LlmError"]:
        return
    reply = LLM["_lift"](adapter().parse_response(raw), T(tool_mode="fenced"))
    assert reply["stop"] == "length"
    assert not any(p["type"] == "tool_call" for p in reply["parts"])


def test_order_phase_call_identity_opaque_roundtrip_and_switch():
    r = parse([reasoning(), message("working", "commentary"), call(), message()])
    assert [p["type"] for p in r["parts"]] == ["thinking", "text", "tool_call", "text"]
    assert r["parts"][2]["id"] == "call_actual"
    assert r["usage"] == {"in": 5, "out": 12, "cacheRead": 20, "cacheWrite": 0}
    msgs = [
        {"role": "assistant", "parts": r["parts"]},
        {
            "role": "user",
            "parts": [{"type": "tool_result", "call_id": "call_actual", "content": "2"}],
        },
    ]
    req = adapter().build_request(msgs, "SYS", [{"name": "run_cell"}], "gpt-synthetic", T())
    assert "lookup_" not in json.dumps(req)
    assert req["input"][0] == {k: v for k, v in reasoning().items() if k != "id"}
    assert req["input"][1]["phase"] == "commentary"
    assert req["input"][3]["phase"] == "final_answer"
    assert req["input"][4]["call_id"] == "call_actual"
    for key, value in (("backend", "foreign"), ("endpoint", BASE + "/other"), ("model", "other")):
        foreign = copy.deepcopy(msgs)
        foreign[0]["parts"][0]["provider_state"][key] = value
        wire = adapter().build_request(foreign, "", [], "gpt-synthetic", T())
        assert "OPAQUE" not in json.dumps(wire)
    lifted = LLM["_lift"]({"parts": [r["parts"][0]], "stop": "done", "usage": {}}, T())
    assert lifted["parts"] == [r["parts"][0]]


@pytest.mark.parametrize(
    "args",
    [
        '{"code":',
        "[]",
        "null",
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e999}',
        '{"code":"a","code":"b"}',
        '"hello"',
    ],
)
def test_incomplete_or_nonobject_arguments_never_execute(args):
    with pytest.raises(LLM["IncompleteReply"]):
        parse([call(args)])


@pytest.mark.parametrize(
    "mode,text",
    [("fenced", "```cell\nexplode()\n```"), ("xml", '<tool_call>{"name":"run_cell"}</tool_call>')],
)
def test_output_limit_cannot_be_lifted_to_a_call(mode, text):
    r = parse([message(text), call()], status="incomplete")
    r = LLM["_lift"](r, T(tool_mode=mode))
    assert r["stop"] == "length" and not any(p["type"] == "tool_call" for p in r["parts"])


def test_refusal_missing_usage_and_failed_response():
    m = message()
    m["content"] = [{"type": "refusal", "refusal": "Cannot help."}]
    assert parse([m], usage=False)["usage"]["missing"] is True
    assert parse([m])["parts"][0]["text"] == "Cannot help."
    with pytest.raises(LLM["LlmError"]):
        parse([], status="failed")


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_terminal",
        "missing_terminal",
        "identity",
        "unfinished",
        "changed_terminal",
        "reordered",
        "duplicate_item",
        "event_name",
        "truncated",
        "sequence",
        "delta_after_done",
        "invalid_json",
    ],
)
def test_malformed_streams_fail_closed(mutation):
    ev = events([message(), call()])
    if mutation == "duplicate_terminal":
        ev.append(copy.deepcopy(ev[-1]))
    elif mutation == "missing_terminal":
        ev.pop()
    elif mutation == "identity":
        ev[-1]["response"]["id"] = "foreign"
    elif mutation == "unfinished":
        ev[2]["item"]["status"] = "in_progress"
        ev[-1]["response"]["output"][0]["status"] = "in_progress"
    elif mutation == "changed_terminal":
        ev[-1]["response"]["output"][0]["content"][0]["text"] = "different"
    elif mutation == "reordered":
        ev[1]["output_index"] = 1
    elif mutation == "duplicate_item":
        ev.insert(2, copy.deepcopy(ev[1]))
    elif mutation == "sequence":
        for e in ev:
            e["sequence_number"] = 0
    elif mutation == "delta_after_done":
        ev.insert(
            3,
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "item_id": "lookup_final_answer",
                "delta": "bad",
            },
        )
    text = stream(ev)
    if mutation == "event_name":
        text = text.replace("event: response.completed", "event: response.failed")
    elif mutation == "truncated":
        text = text[:-1]
    elif mutation == "invalid_json":
        text += "data: {bad}\n\n"
    with pytest.raises(LLM["IncompleteReply"]):
        adapter().parse_stream(text)


def test_delta_identity_and_final_consistency():
    ev = events([message()])
    ev[1]["item"]["content"] = []
    ev[2:2] = [
        {
            "type": "response.output_text.delta",
            "item_id": "lookup_final_answer",
            "output_index": 0,
            "content_index": 0,
            "delta": "answer",
        },
        {
            "type": "response.output_text.done",
            "item_id": "lookup_final_answer",
            "output_index": 0,
            "content_index": 0,
            "text": "answer",
        },
    ]
    assert adapter().parse_stream(stream(ev))["status"] == "completed"
    ev[2]["item_id"] = "other"
    with pytest.raises(LLM["IncompleteReply"]):
        adapter().parse_stream(stream(ev))


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": {"store": True}},
        {"options": {"input": []}},
        {"stream": False},
        {"options": {"temperature": 0}},
        {"options": {"max_output_tokens": 100}},
        {"options": {"reasoning": {"effort": "invalid"}}},
        {"options": {"text": {"verbosity": "low", "format": {}}}},
        {"provider": "openai-compat"},
        {"backend": "openai"},
        {"base_url": "https://evil.test"},
        {"api_key_ref": "llm.key.openai"},
        {"api_key_ref": None},
    ],
)
def test_configuration_fails_before_network(overrides):
    def effect(name, payload):
        assert name == "config.get"
        return {"value": {**PROV, **overrides}}

    llm = load(effect)
    with pytest.raises(llm["ConfigError"]):
        llm["chat"]([], max_tokens=5)


def test_chat_core_only_blob_image_options_and_host_billing():
    posts = []

    def effect(name, payload):
        if name == "config.get":
            return {"value": {**PROV, "options": {"reasoning": {"effort": "high"}}}}
        if name == "time.now":
            return {"epoch": 1234}
        assert name == "http.post"
        posts.append(payload)
        return {
            "status": 200,
            "body": stream(events([message()])),
            "billingSource": "chatgpt-subscription",
        }

    llm = load(effect)
    ref = {"__blob": "sha256:synthetic", "bytes": 3, "mime": "image/png"}
    r = llm["chat"](
        [{"role": "user", "parts": [{"type": "file", "media_type": "image/png", "data": ref}]}],
        max_tokens=1,
    )
    p = posts[0]
    assert p["credential"] == {"ref": REF} and p["url"] == BASE + "/responses"
    assert set(p["json"]) == {
        "model",
        "instructions",
        "input",
        "tools",
        "store",
        "stream",
        "include",
        "reasoning",
    }
    assert p["json"]["input"][0]["content"][0]["image_url"] == {**ref, "encoding": "data-uri"}
    assert r["usage"]["billingSource"] == "chatgpt-subscription"
    with pytest.raises(llm["UnsupportedMedia"]):
        llm["chat"](
            [
                {
                    "role": "user",
                    "parts": [{"type": "file", "media_type": "audio/wav", "data": "AAAA"}],
                }
            ]
        )
    assert len(posts) == 1


def test_retry_deadline_covers_backoff_and_fold():
    now, calls = [0], []

    def effect(name, payload):
        if name == "config.get":
            return {"value": {**PROV, "timeout": 3}}
        if name == "time.now":
            return {"epoch": now[0]}
        if name == "sleep":
            now[0] += payload["seconds"]
            return {}
        calls.append(payload)
        now[0] += 2
        return {"status": 429, "body": "safe", "headers": {}}

    llm = load(effect)
    with pytest.raises(llm["LlmError"], match="deadline"):
        llm["chat"]([])
    assert len(calls) == 1


def test_actual_loop_preserves_live_parts_and_durable_boot_is_display_only():
    from test_history_module import H
    from test_toolcaller import World, run

    wire = []
    replies = [
        stream(events([reasoning(), message("working", "commentary"), call()])),
        stream(events([message("2")])),
    ]

    def effect(name, payload):
        if name == "config.get":
            return {"value": PROV}
        if name == "time.now":
            return {"epoch": 1}
        assert name == "http.post"
        wire.append(copy.deepcopy(payload))
        return {"status": 200, "body": replies.pop(0), "billingSource": "chatgpt-subscription"}

    llm = load(effect)
    w = World([])
    original = w.use
    w.use = lambda spec: (
        SimpleNamespace(chat=llm["chat"], profile=llm["profile"])
        if spec == "llm@v1"
        else original(spec)
    )
    result = run(w)
    assert result["replies"] == ["2"]
    second = wire[1]["json"]["input"]
    assert [i["type"] for i in second[-4:]] == [
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert second[-3]["phase"] == "commentary"
    assert second[-2]["call_id"] == second[-1]["call_id"] == "call_actual"
    assert "2" in second[-1]["output"]
    assert "OPAQUE" not in json.dumps(w.turns)
    assert w.turns[0]["llm"]["billingSource"] == "chatgpt-subscription"
    turns = [{**w.turns[0], "seq": 0}]
    boot = H["render_boot_window"](turns, {}, total_tokens=1000)
    rebuilt = adapter().build_request(boot, "", [], "gpt-synthetic", T())
    assert "OPAQUE" not in json.dumps(rebuilt) and "working" not in json.dumps(rebuilt)
    turns = [{**turns[0], "seq": i, "userText": "q" * 1000} for i in range(30)]
    compact = H["render_boot_window"](
        turns,
        {1: [{"seq": 0, "level": 1, "fromSeq": 0, "toSeq": 9, "summary": "display-only summary"}]},
        total_tokens=500,
    )
    assert "OPAQUE" not in json.dumps(compact)


@pytest.mark.parametrize("terminal_output", ["full", "missing", "null", "empty"])
def test_real_kernel_toolcaller_executes_cell_with_image_and_phase_roundtrip(terminal_output):
    from kernelenv import load_kernel
    from test_toolcaller import World

    w = World([])
    client = w.use("any@v1")
    client.chat_log = lambda space, chat: {"objectId": chat}
    client.get_skill = lambda _name: ""
    posts = []
    replies = [
        stream(
            backend_events(
                [
                    {**reasoning(), "summary": [{"type": "summary_text", "text": "Compute"}]},
                    message("working", "commentary"),
                    call('{"code":"computed = 6 * 7\\ncomputed"}'),
                ], terminal_output,
            )
        ),
        stream(backend_events([message("42")], terminal_output)),
    ]

    def effect(name, payload):
        if name == "config.get":
            return {"value": PROV}
        if name == "runtime.get":
            return {"value": None}
        if name == "time.now":
            return {"epoch": 1}
        if name == "mailbox.drain":
            return {"items": []}
        if name == "trace.effects_of":
            return {"records": []}
        assert name == "http.post", (name, payload)
        posts.append(payload)
        return {"status": 200, "body": replies.pop(0), "billingSource": "chatgpt-subscription"}

    image = {
        "role": "user",
        "parts": [{"type": "file", "media_type": "image/png", "data": "c3ludGhldGlj"}],
    }
    ar = (
        f"def plan(*args, **kwargs):\n    return {{'messages': [{image!r}], 'injected': []}}\n"
        "def log_roi(*args):\n    pass\n"
    )
    kernel = load_kernel(
        effect=effect,
        any_client=client,
        module_source=lambda spec: ar if spec == "autorecall@v1" else None,
    )
    result = kernel._run_cell(
        "use('toolcaller@v1').main({'space':'s1', 'chatId':'c1', 'userText':'calculate'})", "main"
    )
    assert result["ok"], result["error"]
    assert kernel._ns["computed"] == 42
    assert len(posts) == 2
    assert "input_image" in json.dumps(posts[0])
    assert posts[1]["json"]["input"][-3]["phase"] == "commentary"
    assert posts[1]["json"]["input"][-1]["call_id"] == "call_actual"
    assert "42" in posts[1]["json"]["input"][-1]["output"]
    assert w.turns[0]["replies"] == ["42"]
    assert w.turns[0]["llm"]["billingSource"] == "chatgpt-subscription"
    assert "OPAQUE" not in json.dumps(w.turns)


@pytest.mark.parametrize("subscription_first", [False, True])
@pytest.mark.parametrize("usage_mode", ["normal", "zero", "missing"])
def test_real_kernel_loop_tracks_mixed_sources_in_both_orders(subscription_first, usage_mode):
    from kernelenv import load_kernel
    from test_toolcaller import World

    world = World([])
    client = world.use("any@v1")
    client.chat_log = lambda space, chat: {"objectId": chat}
    client.get_skill = lambda _name: ""
    api = {
        "provider": "anthropic", "backend": "anthropic", "model": "claude-sonnet-5",
        "base_url": "https://api.anthropic.com", "api_key_ref": "llm.key.anthropic",
    }
    providers = [PROV, api] if subscription_first else [api, PROV]
    posts = []

    def effect(name, payload):
        if name == "config.get":
            return {"value": providers[min(len(posts), 1)]}
        if name == "runtime.get":
            return {"value": None}
        if name == "time.now":
            return {"epoch": 1}
        if name == "mailbox.drain":
            return {"items": []}
        if name == "trace.effects_of":
            return {"records": []}
        assert name == "http.post", (name, payload)
        i = len(posts)
        posts.append(payload)
        if providers[i] == PROV:
            ev = events([call()] if i == 0 else [message("2")], usage=usage_mode != "missing")
            if usage_mode == "zero":
                ev[-1]["response"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
            return {"status": 200, "body": stream(ev), "billingSource": "chatgpt-subscription"}
        content = (
            [{"type": "tool_use", "id": "api_call", "name": "run_cell", "input": {"code": "1+1"}}]
            if i == 0 else [{"type": "text", "text": "2"}]
        )
        response = {"content": content, "stop_reason": "tool_use" if i == 0 else "end_turn"}
        if usage_mode != "missing":
            response["usage"] = {
                "input_tokens": 100 if usage_mode == "normal" else 0,
                "output_tokens": 30 if usage_mode == "normal" else 0,
            }
        return {"status": 200, "body": json.dumps(response)}

    kernel = load_kernel(effect=effect, any_client=client)
    result = kernel._run_cell(
        "use('toolcaller@v1').main({'space':'s1','chatId':'c1','userText':'calculate'})", "main"
    )
    assert result["ok"], result["error"]
    assert len(posts) == 2
    assert ("chatgpt.com" in posts[0]["url"]) == subscription_first
    stats = world.turns[0]["llm"]
    assert stats["billingSource"] == "mixed" and stats["costUsd"] is None
    assert stats["inTokens"] == (105 if usage_mode == "normal" else 0)
    assert stats["outTokens"] == (42 if usage_mode == "normal" else 0)
    assert bool(stats.get("usageMissing")) == (usage_mode == "missing")
