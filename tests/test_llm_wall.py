"""llm@v1 — the wall (ADR-035 §1, §5): every well-formed `any://` link
in the text of a model call is a short handle on the wire, and every
handle in the reply is a link again. The key rule, the link shapes and
the kinds are asserted on the pure functions; the placement (handles in
the `http.post` body, links in what `chat` returns) end to end."""

import json

from test_llm_module import LLM, FakeHost, load

HEAD = "bafyrei"            # the constant header an id drops: 7 characters
FILL = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwxyz"


def oid(tail):
    """An object id (59 characters) whose text after the header starts
    with `tail`."""
    return HEAD + (tail + FILL)[:52]


def sid(tail, suffix="6n6sn58q1zmv"):
    return oid(tail) + "." + suffix


S = sid("a3nqgj")
DUNE, EMMA = oid("gyuur4hq"), oid("k7mzzz")
FILE = "3V2A2AJ6J8j"
IDENT = "A7" + "hQ66Mo" + "x" * 40


def link(o, s=S):
    return f"any://o/{s}/{o}"


def inward(text, table=None):
    table = table if table is not None else LLM["_wall_table"]()
    msgs, _ = LLM["_wall_in"](table, [{"role": "user", "parts": [
        {"type": "text", "text": text}]}], "")
    return msgs[0]["parts"][0]["text"], table


def outward(table, text):
    reply = LLM["_wall_out"](table, {"parts": [{"type": "text", "text": text}],
                                     "stop": "done"})
    return reply["parts"][0]["text"], reply.get("unresolvedRefs")


# --- the key rule (§1) --------------------------------------------------------

def test_key_is_three_characters_after_the_header():
    text, _ = inward(f"{link(DUNE)} any://f/{S}/{FILE} any://s/{S}")
    assert text == "ref:object/gyu ref:file/3V2 ref:space/a3n"


def test_the_dot_of_a_space_id_is_not_a_key_character():
    # the hash is all `a`, so the slides run into the suffix
    space = HEAD + "a" * 52 + ".xyz"
    table = LLM["_wall_table"]()
    table["keys"][("space", "aaa")] = {"id": "other"}
    text, _ = inward(space, table)
    assert text == "ref:space/aax"     # …aa.xyz slides to `aax`, not `aa.`


def test_first_id_seen_keeps_its_key_and_the_later_one_slides():
    first, second = oid("gyuaaaa"), oid("gyubbbb")
    text, table = inward(f"{link(first)} {link(second)} {link(first)}")
    assert text == "ref:object/gyu ref:object/yub ref:object/gyu"
    # the other order gives the other id the plain slice
    text, _ = inward(f"{link(second)} {link(first)}")
    assert text == "ref:object/gyu ref:object/yua"
    # 2 ids, 1 slide: three questions to the key set
    assert table["lookups"] == 3


def test_a_second_collision_slides_again():
    a, b, c = oid("gyuaaaa"), oid("yubzzzz"), oid("gyubqqq")
    text, table = inward(f"{link(a)} {link(b)} {link(c)}")
    # c: `gyu` is a's, `yub` is b's, `ubq` is free
    assert text == "ref:object/gyu ref:object/yub ref:object/ubq"
    assert table["lookups"] == 3 + 2


def test_two_spaces_with_one_hash_part_after_one_step():
    a, b = sid("a3nqgj", "aaaa"), sid("a3nqgj", "bbbb")
    text, _ = inward(f"{a} {b}")
    assert text == "ref:space/a3n ref:space/3nq"


def test_every_slice_taken_extends_the_first_slice():
    table = LLM["_wall_table"]()
    f = "ABCDEFGH"                                   # six slices
    for i in range(6):
        table["keys"][("file", f[i:i + 3])] = {"space": S, "id": "x"}
    table["keys"][("file", "ABCD")] = {"space": S, "id": "y"}
    text, _ = inward(f"any://f/{S}/{f}", table)
    assert text == "ref:file/ABCDE"
    assert table["lookups"] == 6 + 2


def test_kinds_do_not_collide_with_each_other():
    obj = oid("3v2a2a")                               # the file's slice
    text, table = inward(f"any://f/{S}/3v2a2aj6j8 {link(obj)}")
    assert text == "ref:file/3v2 ref:object/3v2"
    assert table["lookups"] == 2                      # the file, the object


def test_identity_keys_after_its_first_two_characters():
    text, table = inward(f"[tolya](any://u/{IDENT})")
    assert text == "[tolya](ref:user/hQ6)"
    assert outward(table, "ask [tolya](ref:user/hQ6)")[0] == f"ask [tolya](any://u/{IDENT})"


# --- only a well-formed link is rewritten (§5) --------------------------------

def test_things_that_are_not_links_of_the_registry_pass_through():
    cut = DUNE[:40]
    texts = [
        "any://o/<spaceId>/<id>",                     # a placeholder
        "any://f/{sid}/{k}",
        f"any://o/{S}/{cut}",                         # an id cut short
        f"any://o/{S}/{cut}…",
        f"any://f/{S}/abc",                           # a short file id
        f"any://f/{S}/{FILE}?variant=thumb",          # a query
        f"any://o/{S}/{DUNE}/editor_blocks",          # a path the registry lacks
        f"any://o/{S}/{DUNE}/a/b/c",
        f"any://m/{S}/{IDENT}",                       # the legacy member link
        f"any://r/{S}/{DUNE}",                        # an unknown kind
        f"any://{DUNE}",                              # the legacy value form
        f"/v1/spaces/{S}/query",                      # an id inside a path
    ]
    for text in texts:
        out, table = inward(text)
        assert out == text, text
        assert table["ids"] == {} and table["lookups"] == 0, text


def test_a_link_ends_at_punctuation_and_keeps_its_fragment():
    text, _ = inward(f"See [Dune]({link(DUNE)}#book), then {link(EMMA)}. Or \"{link(DUNE)}\"")
    assert text == ('See [Dune](ref:object/gyu#book), then ref:object/k7m. '
                    'Or "ref:object/gyu"')


# --- the kinds (§1) -----------------------------------------------------------

def test_record_and_property_links_keep_their_tail_both_ways():
    text, table = inward(f"{link(DUNE)}/chat_messages/m_1-x any://p/{S}/{DUNE}/rating")
    assert text == "ref:object/gyu/chat_messages/m_1-x ref:prop/gyu/rating"
    # the table has the object once and no entry for a tail
    assert list(table["ids"]) == [("object", DUNE)]
    back, unresolved = outward(table, text)
    assert back == f"{link(DUNE)}/chat_messages/m_1-x any://p/{S}/{DUNE}/rating"
    assert unresolved is None


def test_block_citation_list_is_rewritten_link_by_link():
    cite = f"{link(DUNE)}/editor_blocks/b1,{link(DUNE)}/editor_blocks/b2"
    text, table = inward(cite)
    assert text == "ref:object/gyu/editor_blocks/b1,ref:object/gyu/editor_blocks/b2"
    assert outward(table, text)[0] == cite


def test_space_handle_is_a_link_in_markdown_and_an_id_elsewhere():
    text, table = inward(f"[Garden](any://s/{S}) and {S}")
    assert text == "[Garden](ref:space/a3n) and ref:space/a3n"
    back, _ = outward(table, 'Open [Garden](ref:space/a3n); c.list("ref:space/a3n")')
    assert back == f'Open [Garden](any://s/{S}); c.list("{S}")'


def test_stamp_ids_become_handles():
    stamp = f"[now: Thu 2026-11-12 10:00 | user's view — space: {S}, object: {DUNE}, view: page]"
    text, table = inward(stamp)
    assert text == ("[now: Thu 2026-11-12 10:00 | user's view — space: ref:space/a3n, "
                    "object: ref:object/gyu, view: page]")
    assert outward(table, "ref:object/gyu")[0] == link(DUNE)


def test_bare_object_id_is_a_handle_only_after_the_call_showed_it():
    text, _ = inward(f"{EMMA} then {link(DUNE)} then {DUNE} and {EMMA}")
    assert text == f"{EMMA} then ref:object/gyu then ref:object/gyu and {EMMA}"


# --- in and out (§5) ----------------------------------------------------------

def test_inward_is_a_function_of_the_text_and_an_extension_extends():
    window = f"Added [Dune]({link(DUNE)}) to {S}. File any://f/{S}/{FILE}."
    more = window + f" Then [Emma]({link(EMMA)}) and {link(oid('gyuzzzz'))}."
    a, _ = inward(window)
    b, _ = inward(window)
    longer, _ = inward(more)
    assert a == b
    assert longer.startswith(a)


def test_fragment_is_dropped_outward():
    _, table = inward(link(DUNE))
    assert outward(table, "[Dune](ref:object/gyu#novel)")[0] == f"[Dune]({link(DUNE)})"


def test_unknown_handle_is_named_and_listed():
    _, table = inward(link(DUNE))
    text, unresolved = outward(table, "ref:object/gyu ref:object/gyx ref:file/gyu ref:object/gyx")
    assert text == f"{link(DUNE)} ref:unknown/gyx ref:unknown/gyu ref:unknown/gyx"
    assert unresolved == ["ref:object/gyx", "ref:file/gyu"]


def test_a_damaged_key_misses_the_table():
    _, table = inward(link(DUNE))
    for damaged in ("ref:object/gy", "ref:object/gyuu", "ref:object/Gyu"):
        text, unresolved = outward(table, damaged)
        assert text.startswith("ref:unknown/") and unresolved == [damaged]


def test_handle_shaped_text_in_the_input_never_expands():
    text, table = inward(f"the user typed ref:object/gyu, then {link(DUNE)}")
    assert text == "the user typed ref:unknown/gyu, then ref:object/gyu"
    # `unknown` is not a kind: the outward rule leaves it and lists nothing
    assert outward(table, "ref:unknown/gyu") == ("ref:unknown/gyu", None)


def test_every_part_kind_is_covered_and_thinking_is_not_touched():
    table = LLM["_wall_table"]()
    signed = {"type": "thinking", "thinking": f"saw {link(DUNE)}", "signature": "sig"}
    thinking = {"type": "thinking", "text": f"saw {link(DUNE)}", "provider_state": signed}
    file_part = {"type": "file", "media_type": "image/png", "data": "aGk="}
    msgs = [
        {"role": "user", "parts": [file_part, {"type": "text", "text": link(DUNE), "cache": True}]},
        {"role": "assistant", "parts": [
            thinking,
            {"type": "tool_call", "id": "c1", "name": "run_cell",
             "args": {"code": f'getRef("{link(DUNE)}")', "n": 1}}]},
        {"role": "user", "parts": [
            {"type": "tool_result", "call_id": "c1",
             "content": json.dumps({"uri": link(EMMA), "spaceId": S})}]},
    ]
    before = json.dumps(msgs)
    out, system = LLM["_wall_in"](table, msgs, ["rules", f"space {S}"])
    assert json.dumps(msgs) == before                 # the caller's messages are not changed
    assert system == ["rules", "space ref:space/a3n"]
    assert out[0]["parts"] == [file_part, {"type": "text", "text": "ref:object/gyu", "cache": True}]
    assert out[1]["parts"][0] == thinking
    assert out[1]["parts"][1]["args"] == {"code": 'getRef("ref:object/gyu")', "n": 1}
    # a uri first seen in a tool_result is a handle, and expands from the reply
    assert json.loads(out[2]["parts"][0]["content"]) == {"uri": "ref:object/k7m",
                                                         "spaceId": "ref:space/a3n"}
    reply = LLM["_wall_out"](table, {"stop": "tool", "parts": [
        {"type": "thinking", "text": "ref:object/k7m",
         "provider_state": {"thinking": "ref:object/k7m"}},
        {"type": "tool_call", "id": "c2", "name": "run_cell", "args": {
            "code": 'd = getRef("ref:object/k7m")\nc.put_markdown("ref:space/a3n", '
                    'd["objectId"], "see [Dune](ref:object/gyu)")'}}]})
    assert reply["parts"][0] == {"type": "thinking", "text": "ref:object/k7m",
                                 "provider_state": {"thinking": "ref:object/k7m"}}
    assert reply["parts"][1]["args"]["code"] == (
        f'd = getRef("{link(EMMA)}")\nc.put_markdown("{S}", '
        f'd["objectId"], "see [Dune]({link(DUNE)})")')
    assert "unresolvedRefs" not in reply


# --- the placement: handles in http.post, links in what chat returns ----------

def _anthropic_reply(*blocks):
    return json.dumps({"content": list(blocks), "stop_reason": "end_turn",
                       "usage": {"input_tokens": 1, "output_tokens": 1}})


PROV = {"provider": "anthropic", "model": "claude-x", "base_url": "https://api.example/",
        "api_key_ref": "llm.keys.anthropic", "stream": False}


def test_chat_sends_handles_and_returns_links():
    host = FakeHost(PROV, body=_anthropic_reply(
        {"type": "text", "text": "Added [Dune](ref:object/gyu#book) in [Garden](ref:space/a3n); "
                                 "see ref:object/zzz"}))
    msgs = [{"role": "user", "parts": [{"type": "text", "text":
            f"rate {link(DUNE)}\n\n[now: Thu 2026-11-12 10:00 | user's view — space: {S}]"}]}]
    reply = load(host)["chat"](msgs, system=f"home {S}")
    body = json.dumps(host.posts[0]["json"])
    assert "any://" not in body and S not in body and DUNE not in body
    assert "ref:object/gyu" in body and "ref:space/a3n" in body
    assert reply["parts"][0]["text"] == (
        f"Added [Dune]({link(DUNE)}) in [Garden](any://s/{S}); see ref:unknown/zzz")
    assert reply["unresolvedRefs"] == ["ref:object/zzz"]
    assert DUNE in msgs[0]["parts"][0]["text"]        # the caller keeps its links


def test_chat_without_ids_has_no_unresolved_field():
    host = FakeHost(PROV, body=_anthropic_reply({"type": "text", "text": "hello"}))
    reply = load(host)["chat"]([{"role": "user", "parts": [{"type": "text", "text": "hi"}]}])
    assert reply["parts"] == [{"type": "text", "text": "hello"}]
    assert "unresolvedRefs" not in reply
