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

def test_key_is_five_characters_after_the_header():
    text, _ = inward(f"{link(DUNE)} any://f/{S}/{FILE} any://s/{S}")
    assert text == "ref:object/gyuur ref:file/3V2A2 ref:space/a3nqg"


def test_object_key_is_the_short_id_of_the_server():
    # any docs/03-api.md § Object ids: the part of the id after `bafyrei`
    _, table = inward(link(DUNE))
    (_, key), = table["keys"]
    assert DUNE.startswith("bafyrei" + key) and len(key) == 5


def test_key_is_a_function_of_the_id_alone():
    # the text before an id does not change its key
    other = oid("gyuaaaa")
    assert inward(link(DUNE))[0] == "ref:object/gyuur"
    assert inward(f"{link(other)} {link(EMMA)} {link(DUNE)}")[0].endswith("ref:object/gyuur")


def test_first_id_seen_keeps_its_key_and_the_later_one_stays_long():
    first, second = oid("gyuuraaa"), oid("gyuurbbb")
    text, table = inward(f"{link(first)} {link(second)} {link(first)} {second}")
    assert text == f"ref:object/gyuur {link(second)} ref:object/gyuur {second}"
    assert table["ids"] == {("object", first): "gyuur", ("object", second): None}
    # the key names the first id only
    assert outward(table, "ref:object/gyuur")[0] == link(first)
    # the other order gives the other id the key
    text, _ = inward(f"{link(second)} {link(first)}")
    assert text == f"ref:object/gyuur {link(first)}"


def test_two_spaces_with_one_hash_the_later_one_stays_long():
    a, b = sid("a3nqgj", "aaaa"), sid("a3nqgj", "bbbb")
    text, _ = inward(f"{a} {b}")
    assert text == f"ref:space/a3nqg {b}"


def test_a_stamp_with_an_id_that_has_no_key_stays_as_written():
    first, second = oid("gyuuraaa"), oid("gyuurbbb")
    stamp = f"space: {S}, object: {second}"
    text, _ = inward(f"{link(first)} [{stamp}]")
    assert text == f"ref:object/gyuur [{stamp}]"


def test_kinds_do_not_collide_with_each_other():
    obj = oid("3v2a2a")                               # the file's key
    text, _ = inward(f"any://f/{S}/3v2a2aj6j8 {link(obj)}")
    assert text == "ref:file/3v2a2 ref:object/3v2a2"


def test_identity_keys_after_its_first_two_characters():
    text, table = inward(f"[tolya](any://u/{IDENT})")
    assert text == "[tolya](ref:user/hQ66M)"
    assert outward(table, "ask [tolya](ref:user/hQ66M)")[0] == f"ask [tolya](any://u/{IDENT})"


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
        assert table["ids"] == {}, text


def test_a_link_ends_at_punctuation_and_keeps_its_fragment():
    text, _ = inward(f"See [Dune]({link(DUNE)}#book), then {link(EMMA)}. Or \"{link(DUNE)}\"")
    assert text == ('See [Dune](ref:object/gyuur#book), then ref:object/k7mzz. '
                    'Or "ref:object/gyuur"')


# --- the kinds (§1) -----------------------------------------------------------

def test_record_and_property_links_keep_their_tail_both_ways():
    text, table = inward(f"{link(DUNE)}/chat_messages/m_1-x any://p/{S}/{DUNE}/rating")
    assert text == "ref:object/gyuur/chat_messages/m_1-x ref:prop/gyuur/rating"
    # the table has the object once and no entry for a tail
    assert list(table["ids"]) == [("object", DUNE)]
    back, unresolved = outward(table, text)
    assert back == f"{link(DUNE)}/chat_messages/m_1-x any://p/{S}/{DUNE}/rating"
    assert unresolved is None


def test_block_citation_list_is_rewritten_link_by_link():
    cite = f"{link(DUNE)}/editor_blocks/b1,{link(DUNE)}/editor_blocks/b2"
    text, table = inward(cite)
    assert text == "ref:object/gyuur/editor_blocks/b1,ref:object/gyuur/editor_blocks/b2"
    assert outward(table, text)[0] == cite


def test_space_handle_is_a_link_in_markdown_and_an_id_elsewhere():
    text, table = inward(f"[Garden](any://s/{S}) and {S}")
    assert text == "[Garden](ref:space/a3nqg) and ref:space/a3nqg"
    back, _ = outward(table, 'Open [Garden](ref:space/a3nqg); c.list("ref:space/a3nqg")')
    assert back == f'Open [Garden](any://s/{S}); c.list("{S}")'


def test_stamp_ids_become_handles():
    stamp = f"[now: Thu 2026-11-12 10:00 | user's view — space: {S}, object: {DUNE}, view: page]"
    text, table = inward(stamp)
    assert text == ("[now: Thu 2026-11-12 10:00 | user's view — space: ref:space/a3nqg, "
                    "object: ref:object/gyuur, view: page]")
    assert outward(table, "ref:object/gyuur")[0] == link(DUNE)


def test_bare_object_id_is_a_handle_and_comes_back_as_the_id():
    # no link of the call names EMMA: its handle has no space, and it
    # is the bare id again wherever the model writes it (ADR-035 §5)
    text, table = inward(f"{EMMA} then {link(DUNE)} then {DUNE} and {EMMA}")
    assert text == ("ref:object/k7mzz then ref:object/gyuur then ref:object/gyuur "
                    "and ref:object/k7mzz")
    out, bad = outward(table, 'get(s, "ref:object/k7mzz"); {"ref:object/k7mzz": 1}')
    assert out == f'get(s, "{EMMA}"); {{"{EMMA}": 1}}' and not bad
    # DUNE has a link, so it is the link as before
    assert outward(table, "ref:object/gyuur")[0] == link(DUNE)


def test_a_later_link_gives_a_bare_id_its_space():
    # the id stands bare first (a printed `id`), a link follows (a row's
    # `uri`): one key, and it expands to the link
    text, table = inward(f"made {EMMA}; row {link(EMMA)}")
    assert text == "made ref:object/k7mzz; row ref:object/k7mzz"
    assert outward(table, "[Emma](ref:object/k7mzz)") == (f"[Emma]({link(EMMA)})", None)


def test_a_bare_id_handle_has_no_link_to_become():
    _, table = inward(f"made {EMMA} in {S}")
    # as a link target, and as a property handle: unresolved, listed
    assert outward(table, "[Emma](ref:object/k7mzz)") == (
        "[Emma](ref:unknown/k7mzz)", ["ref:object/k7mzz"])
    assert outward(table, "ref:prop/k7mzz/status")[1] == ["ref:prop/k7mzz"]
    # a typed link that names the space gives it one
    assert outward(table, "any://o/a3nqg/k7mzz")[0] == link(EMMA)
    assert outward(table, f"any://o/{S}/k7mzz")[0] == link(EMMA)


def test_a_bare_id_inside_a_longer_name_stays():
    for text in (f"/objects/{EMMA}", f"{EMMA}_agent_memory_items"):
        assert inward(text)[0] == text


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
    assert outward(table, "[Dune](ref:object/gyuur#novel)")[0] == f"[Dune]({link(DUNE)})"


def test_unknown_handle_is_named_and_listed():
    _, table = inward(link(DUNE))
    text, unresolved = outward(
        table, "ref:object/gyuur ref:object/gyuux ref:file/gyuur ref:object/gyuux")
    assert text == f"{link(DUNE)} ref:unknown/gyuux ref:unknown/gyuur ref:unknown/gyuux"
    assert unresolved == ["ref:object/gyuux", "ref:file/gyuur"]


def test_a_damaged_key_misses_the_table():
    _, table = inward(link(DUNE))
    for damaged in ("ref:object/gyuu", "ref:object/gyuur4", "ref:object/Gyuur", "ref:object/gyu"):
        text, unresolved = outward(table, damaged)
        assert text.startswith("ref:unknown/") and unresolved == [damaged]


def test_handle_shaped_text_in_the_input_never_expands():
    text, table = inward(f"the user typed ref:object/gyuur, then {link(DUNE)}")
    assert text == "the user typed ref:unknown/gyuur, then ref:object/gyuur"
    # `unknown` is not a kind: the outward rule leaves it and lists nothing
    assert outward(table, "ref:unknown/gyuur") == ("ref:unknown/gyuur", None)


# --- a typed link written with keys (§5) ---------------------------------------

def keyed_table():
    _, table = inward(f"{link(DUNE)} any://f/{S}/{FILE} any://u/{IDENT} any://s/{S}")
    return table


def test_a_link_filled_with_keys_becomes_the_link_of_those_keys():
    table = keyed_table()
    text, unresolved = outward(
        table, "[trace](any://f/a3nqg/3V2A2) any://o/a3nqg/gyuur/editor_blocks/b1 "
               "any://p/a3nqg/gyuur/rating [Garden](any://s/a3nqg) any://u/hQ66M")
    assert text == (f"[trace](any://f/{S}/{FILE}) {link(DUNE)}/editor_blocks/b1 "
                    f"any://p/{S}/{DUNE}/rating [Garden](any://s/{S}) any://u/{IDENT}")
    assert unresolved is None


def test_the_key_of_the_thing_decides_and_a_space_name_is_ignored():
    table = keyed_table()
    assert outward(table, "any://o/Garden/gyuur")[0] == link(DUNE)
    # a key in the space segment of a link that holds a real id
    assert outward(table, f"any://o/a3nqg/{EMMA} any://m/a3nqg/{IDENT}")[0] == \
        f"{link(EMMA)} any://m/{S}/{IDENT}"


def test_a_key_the_table_does_not_hold_beside_a_space_key_is_unknown():
    table = keyed_table()
    text, unresolved = outward(table, "see [x](any://f/a3nqg/WKo). any://f/a3nqg/WKo")
    assert text == "see [x](ref:unknown/WKo). ref:unknown/WKo"
    assert unresolved == ["any://f/a3nqg/WKo"]


def test_a_link_without_a_key_stays_as_written():
    table = keyed_table()
    for text in (f"{link(DUNE)} any://f/{S}/{FILE}",      # well-formed
                 "any://o/spaceId/objectId any://s/Garden",  # a sample, a name
                 "any://o/<spaceId>/<id> any://f/{space}/{fid} any://o/…",
                 f"any://{DUNE}"):                         # the legacy bare form
        assert outward(table, text) == (text, None)


def test_a_dead_link_of_the_input_never_expands():
    # the chat already holds `any://f/a3nqg/3V2A2`: those are not keys of
    # this call, though this call gives the same keys to other things
    text, table = inward(f"any://f/a3nqg/3V2A2 and {link(DUNE)} any://f/{S}/{FILE}")
    assert text == "any://f/a3nqg/3V2A2 and ref:object/gyuur ref:file/3V2A2"
    assert outward(table, "any://f/a3nqg/3V2A2 ref:file/3V2A2") == \
        (f"any://f/a3nqg/3V2A2 any://f/{S}/{FILE}", None)


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
    assert system == ["rules", "space ref:space/a3nqg"]
    assert out[0]["parts"] == [file_part,
                               {"type": "text", "text": "ref:object/gyuur", "cache": True}]
    assert out[1]["parts"][0] == thinking
    assert out[1]["parts"][1]["args"] == {"code": 'getRef("ref:object/gyuur")', "n": 1}
    # a uri first seen in a tool_result is a handle, and expands from the reply
    assert json.loads(out[2]["parts"][0]["content"]) == {"uri": "ref:object/k7mzz",
                                                         "spaceId": "ref:space/a3nqg"}
    reply = LLM["_wall_out"](table, {"stop": "tool", "parts": [
        {"type": "thinking", "text": "ref:object/k7mzz",
         "provider_state": {"thinking": "ref:object/k7mzz"}},
        {"type": "tool_call", "id": "c2", "name": "run_cell", "args": {
            "code": 'd = getRef("ref:object/k7mzz")\nc.put_markdown("ref:space/a3nqg", '
                    'd["objectId"], "see [Dune](ref:object/gyuur)")'}}]})
    assert reply["parts"][0] == {"type": "thinking", "text": "ref:object/k7mzz",
                                 "provider_state": {"thinking": "ref:object/k7mzz"}}
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
        {"type": "text", "text": "Added [Dune](ref:object/gyuur#book) in "
                                 "[Garden](ref:space/a3nqg); see ref:object/zzzzz"}))
    msgs = [{"role": "user", "parts": [{"type": "text", "text":
            f"rate {link(DUNE)}\n\n[now: Thu 2026-11-12 10:00 | user's view — space: {S}]"}]}]
    reply = load(host)["chat"](msgs, system=f"home {S}")
    body = json.dumps(host.posts[0]["json"])
    assert "any://" not in body and S not in body and DUNE not in body
    assert "ref:object/gyuur" in body and "ref:space/a3nqg" in body
    assert reply["parts"][0]["text"] == (
        f"Added [Dune]({link(DUNE)}) in [Garden](any://s/{S}); see ref:unknown/zzzzz")
    assert reply["unresolvedRefs"] == ["ref:object/zzzzz"]
    assert DUNE in msgs[0]["parts"][0]["text"]        # the caller keeps its links


def test_chat_without_ids_has_no_unresolved_field():
    host = FakeHost(PROV, body=_anthropic_reply({"type": "text", "text": "hello"}))
    reply = load(host)["chat"]([{"role": "user", "parts": [{"type": "text", "text": "hi"}]}])
    assert reply["parts"] == [{"type": "text", "text": "hello"}]
    assert "unresolvedRefs" not in reply
