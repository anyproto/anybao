from anybao.toolmd import split_tool_markdown

SAMPLE = """# websearch

## Tool Description

Search the web and return ranked results.
Multi-line body here.

## Tool Schema

### search(query, limit) [getter]

Run a search. Returns hits.

### crawl(url) [mutator]

Fetch and index a page.

### search(query) [getter]

An overload with the same bare name.
"""


def test_extracts_description():
    desc, _ = split_tool_markdown(SAMPLE)
    assert "Search the web and return ranked results." in desc
    assert "Multi-line body here." in desc
    assert "Tool Schema" not in desc  # stops at the next section


def test_extracts_methods_with_kind_and_bare_name():
    _, methods = split_tool_markdown(SAMPLE)
    assert [m.bare_name for m in methods] == ["search", "crawl", "search-2"]
    assert methods[0].name == "search(query, limit)" and methods[0].kind == "getter"
    assert methods[1].kind == "mutator"
    assert "Run a search. Returns hits." in methods[0].text


def test_default_kind_getter():
    _, methods = split_tool_markdown("## Tool Schema\n### noKind(x)\nbody\n")
    assert methods[0].kind == "getter"


def test_no_schema_section():
    desc, methods = split_tool_markdown("## Tool Description\nJust a description.\n")
    assert desc == "Just a description." and methods == []


def test_tools_heading_variants():
    for head in ("# Tools", "## Tools", "## Tool Schema"):
        md = f"## Tool Description\nd\n\n{head}\n{'#' * (head.count('#') + 1)} m(x) [setup]\nbody\n"
        _, methods = split_tool_markdown(md)
        assert methods and methods[0].bare_name == "m" and methods[0].kind == "setup"
