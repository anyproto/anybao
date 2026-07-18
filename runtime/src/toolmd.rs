//! Tool-markdown splitter — the Rust twin of anybao/toolmd.py (plan §4:
//! ONE splitter). Parses a tool-description file into a description
//! body plus per-method docs, matching the canonical format the deploy
//! tool writes to `program_description` / `program_methods`:
//!
//! ```text
//! ## Tool Description
//! <body>
//!
//! ## Tool Schema        (or `# Tools` / `## Tools`)
//! ### method(sig) [kind]
//! <method body>
//! ```
//!
//! `kind` ∈ getter|mutator|setup (default getter). Bare method name
//! (record id) is the text before `(`. Duplicate bare names get a
//! `-<pos>` suffix. Behavior is a verbatim port — the fingerprint in
//! deploy.rs hashes this splitter's output, so parity is load-bearing.
//!
//! `[program]` (bobrik's hide-from-discovery kind) is retired: anybao
//! hides a method by NOT documenting it, not by a kind (ADR-005 §5). An
//! unrecognized `[tag]` is left as part of the heading, not a kind.

const KINDS: [&str; 3] = ["getter", "mutator", "setup"];
const TOOLS_HEADINGS: [&str; 3] = ["# Tools", "## Tools", "## Tool Schema"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MethodDoc {
    /// record id (name before "(")
    pub bare_name: String,
    /// full heading (signature)
    pub name: String,
    /// getter | mutator | setup
    pub kind: String,
    /// method body
    pub text: String,
    /// order
    pub pos: usize,
}

/// `^(#{1,6})\s+(.+)$` → (level, raw title). Mirrors the Python regex
/// including its backtracking edge: an all-whitespace tail of ≥2 chars
/// matches with the last whitespace char as the title.
fn heading(line: &str) -> Option<(usize, &str)> {
    let hashes = line.chars().take_while(|&c| c == '#').count();
    if hashes == 0 || hashes > 6 {
        return None;
    }
    let rest = &line[hashes..];
    let ws: usize = rest
        .chars()
        .take_while(|c| c.is_whitespace())
        .map(char::len_utf8)
        .sum();
    if ws == 0 {
        return None; // no `\s+` after the hashes
    }
    if ws == rest.len() {
        // `\s+(.+)` backtracks: the last whitespace char becomes `.+`
        let last = rest.chars().next_back().expect("rest is non-empty");
        return if rest.chars().count() >= 2 {
            Some((hashes, &rest[rest.len() - last.len_utf8()..]))
        } else {
            None
        };
    }
    Some((hashes, &rest[ws..]))
}

fn extract_section(md: &str, section: &str) -> String {
    let want = section.to_lowercase();
    let mut captured: Vec<&str> = Vec::new();
    let mut capturing = false;
    let mut level = 0usize;
    for line in md.split('\n') {
        if let Some((lvl, title)) = heading(line) {
            if capturing && lvl <= level {
                break;
            }
            if title.trim().to_lowercase() == want {
                capturing = true;
                level = lvl;
                continue;
            }
        }
        if capturing {
            captured.push(line);
        }
    }
    captured.join("\n").trim().to_string()
}

fn bare_name(name: &str) -> &str {
    match name.find('(') {
        Some(i) if i > 0 => name[..i].trim(),
        _ => name.trim(),
    }
}

/// `\s*\[(getter|mutator|setup)\]\s*$` — strip a trailing kind tag;
/// returns (heading-without-tag, kind or None). An unrecognized tag
/// (e.g. the retired `[program]`) is left on the heading, kind None.
fn strip_kind_tag(heading: &str) -> (String, Option<String>) {
    let t = heading.trim_end();
    if t.ends_with(']') {
        if let Some(i) = t.rfind('[') {
            let kind = &t[i + 1..t.len() - 1];
            if KINDS.contains(&kind) {
                return (t[..i].trim().to_string(), Some(kind.to_string()));
            }
        }
    }
    (heading.to_string(), None)
}

/// → (description, methods).
pub fn split_tool_markdown(md: &str) -> (String, Vec<MethodDoc>) {
    let description = extract_section(md, "Tool Description");

    // find the tools/schema section start
    let lines: Vec<&str> = md.split('\n').collect();
    let mut start: Option<usize> = None;
    let mut section_level = 1usize;
    for (line_no, line) in lines.iter().enumerate() {
        let stripped = line.trim();
        if let Some((lvl, _)) = heading(line) {
            if TOOLS_HEADINGS.contains(&stripped) {
                start = Some(line_no);
                section_level = lvl;
                break;
            }
        }
    }
    let Some(start) = start else {
        return (description, Vec::new());
    };

    let method_prefix = format!("{} ", "#".repeat(section_level + 1));
    let mut methods: Vec<MethodDoc> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut cur: Option<MethodDoc> = None;
    let mut cur_lines: Vec<&str> = Vec::new();

    let flush = |cur: &mut Option<MethodDoc>,
                 cur_lines: &mut Vec<&str>,
                 methods: &mut Vec<MethodDoc>,
                 seen: &mut std::collections::HashSet<String>| {
        let Some(mut m) = cur.take() else { return };
        m.text = cur_lines.join("\n").trim_matches('\n').to_string();
        if seen.contains(&m.bare_name) {
            m.bare_name = format!("{}-{}", m.bare_name, m.pos);
        }
        seen.insert(m.bare_name.clone());
        methods.push(m);
        cur_lines.clear();
    };

    for line in &lines[start + 1..] {
        if let Some((lvl, _)) = heading(line) {
            if lvl <= section_level {
                break; // next sibling/parent ends the schema walk
            }
        }
        if let Some(rest) = line.strip_prefix(&method_prefix) {
            flush(&mut cur, &mut cur_lines, &mut methods, &mut seen);
            let (heading, kind) = strip_kind_tag(rest.trim());
            cur = Some(MethodDoc {
                bare_name: bare_name(&heading).to_string(),
                name: heading.clone(),
                kind: kind.unwrap_or_else(|| "getter".to_string()),
                text: String::new(),
                pos: methods.len(),
            });
            cur_lines = Vec::new();
        } else if cur.is_some() {
            cur_lines.push(line);
        }
    }
    flush(&mut cur, &mut cur_lines, &mut methods, &mut seen);
    (description, methods)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "# websearch\n\n## Tool Description\n\n\
Search the web and return ranked results.\nMulti-line body here.\n\n\
## Tool Schema\n\n### search(query, limit) [getter]\n\n\
Run a search. Returns hits.\n\n### crawl(url) [mutator]\n\n\
Fetch and index a page.\n\n### search(query) [getter]\n\n\
An overload with the same bare name.\n";

    #[test]
    fn extracts_description() {
        let (desc, _) = split_tool_markdown(SAMPLE);
        assert!(desc.contains("Search the web and return ranked results."));
        assert!(desc.contains("Multi-line body here."));
        assert!(!desc.contains("Tool Schema")); // stops at the next section
    }

    #[test]
    fn extracts_methods_with_kind_and_bare_name() {
        let (_, methods) = split_tool_markdown(SAMPLE);
        let bares: Vec<&str> = methods.iter().map(|m| m.bare_name.as_str()).collect();
        assert_eq!(bares, ["search", "crawl", "search-2"]);
        assert_eq!(methods[0].name, "search(query, limit)");
        assert_eq!(methods[0].kind, "getter");
        assert_eq!(methods[1].kind, "mutator");
        assert!(methods[0].text.contains("Run a search. Returns hits."));
    }

    #[test]
    fn default_kind_getter() {
        let (_, methods) = split_tool_markdown("## Tool Schema\n### noKind(x)\nbody\n");
        assert_eq!(methods[0].kind, "getter");
        assert_eq!(methods[0].text, "body");
    }

    #[test]
    fn program_kind_retired_tag_stays_on_heading() {
        // [program] is no longer a kind (ADR-005 §5): an unrecognized tag is
        // left as part of the heading, and the method falls back to getter.
        let (_, methods) = split_tool_markdown("## Tool Schema\n### delegate(x) [program]\nbody\n");
        assert_eq!(methods[0].name, "delegate(x) [program]");
        assert_eq!(methods[0].bare_name, "delegate");
        assert_eq!(methods[0].kind, "getter");
    }

    #[test]
    fn no_schema_section() {
        let (desc, methods) = split_tool_markdown("## Tool Description\nJust a description.\n");
        assert_eq!(desc, "Just a description.");
        assert!(methods.is_empty());
    }

    #[test]
    fn tools_heading_variants() {
        for head in TOOLS_HEADINGS {
            let level = head.chars().filter(|&c| c == '#').count();
            let md = format!(
                "## Tool Description\nd\n\n{head}\n{} m(x) [setup]\nbody\n",
                "#".repeat(level + 1)
            );
            let (_, methods) = split_tool_markdown(&md);
            assert!(!methods.is_empty(), "no methods under {head:?}");
            assert_eq!(methods[0].bare_name, "m");
            assert_eq!(methods[0].kind, "setup");
        }
    }

    #[test]
    fn schema_walk_ends_at_sibling_heading() {
        let md = "## Tool Schema\n### go(x)\nbody\n## Next Section\n### notme(y)\nnope\n";
        let (_, methods) = split_tool_markdown(md);
        assert_eq!(methods.len(), 1);
        assert_eq!(methods[0].bare_name, "go");
    }

    #[test]
    fn multi_kind_tags_keep_all_but_last() {
        // matches the Python `\s*\[...\]\s*$` search semantics
        let (_, methods) = split_tool_markdown("# Tools\n## a [getter] [setup]\nb\n");
        assert_eq!(methods[0].name, "a [getter]");
        assert_eq!(methods[0].kind, "setup");
    }

    #[test]
    fn golden_parity_with_python_reference() {
        // exact output the Python splitter produced for this input
        let md = "## Tool Description\nDesc — with unicode ✓\n\n# Tools\n\
## zeta(a) [mutator]\nlast\n## alpha(b)\nfirst\n## alpha(c) [setup]\ndup\n";
        let (desc, ms) = split_tool_markdown(md);
        assert_eq!(desc, "Desc — with unicode ✓");
        let got: Vec<(&str, &str, &str, &str, usize)> = ms
            .iter()
            .map(|m| {
                (
                    m.bare_name.as_str(),
                    m.name.as_str(),
                    m.kind.as_str(),
                    m.text.as_str(),
                    m.pos,
                )
            })
            .collect();
        assert_eq!(
            got,
            [
                ("zeta", "zeta(a)", "mutator", "last", 0),
                ("alpha", "alpha(b)", "getter", "first", 1),
                ("alpha-2", "alpha(c)", "setup", "dup", 2),
            ]
        );
    }
}
