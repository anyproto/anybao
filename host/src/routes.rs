//! Route classification — the Rust twin of anybao/routes.py: boundary-
//! owned read/mutate (+ capability, when grants land here) derived from
//! (method, url).

pub struct Classifier {
    any_netloc: Option<String>,
}

const ANY_READ_POST_SUFFIXES: [&str; 4] = ["/query", "/objects/query", "/search", "/aggregate"];
const LLM_PATH_SUFFIXES: [&str; 3] = ["/v1/messages", "/chat/completions", "/v1/complete"];

fn split_url(url: &str) -> (&str, &str) {
    // (netloc, path) — no external url crate needed for this shape
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    match rest.find('/') {
        Some(i) => (&rest[..i], rest[i..].split('?').next().unwrap_or("")),
        None => (rest, ""),
    }
}

impl Classifier {
    pub fn new(any_base: Option<&str>) -> Self {
        Classifier {
            any_netloc: any_base.map(|b| split_url(b).0.to_string()),
        }
    }

    fn is_any(&self, url: &str) -> bool {
        let (netloc, path) = split_url(url);
        match &self.any_netloc {
            Some(n) => netloc == n,
            None => path.starts_with("/v1/spaces"), // heuristic fallback
        }
    }

    fn is_llm(url: &str) -> bool {
        let (_, path) = split_url(url);
        LLM_PATH_SUFFIXES.iter().any(|s| path.ends_with(s))
    }

    pub fn kind(&self, method: &str, url: &str) -> &'static str {
        if method == "GET" {
            return "read";
        }
        if Self::is_llm(url) {
            return "read"; // model calls replay/mock like any read
        }
        let (_, path) = split_url(url);
        if method == "POST"
            && self.is_any(url)
            && ANY_READ_POST_SUFFIXES.iter().any(|s| path.ends_with(s))
        {
            return "read";
        }
        "mutate"
    }
}
