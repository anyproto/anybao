//! Every program in the repos must pass the deployer's validation
//! (ADR-010 §1 docstring caps, §4 tool shape) — the same `validate`
//! `anyrt deploy` runs, so a refusal is caught here, in CI, not at the
//! prod deploy after the merge (2026-09-14: a one-line docstring growth
//! in gmailSync@v1 was refused on prod; nothing before that step checked).

use anyrt::deploy::load_programs;
use std::path::Path;

#[test]
fn repo_programs_pass_deploy_validation() {
    let repos = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("repos");
    let mut refused = Vec::new();
    let mut seen = 0;
    for repo in ["_agent", "_connectors"] {
        let dir = repos.join(repo).join("programs");
        let programs = load_programs(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display()));
        assert!(!programs.is_empty(), "{}: no programs found", dir.display());
        seen += programs.len();
        for p in &programs {
            if let Err(e) = p.validate() {
                refused.push(format!("{repo}/{}: {e}", p.spec()));
            }
        }
    }
    assert!(
        seen > 20,
        "only {seen} programs loaded — is the repos layout intact?"
    );
    assert!(
        refused.is_empty(),
        "programs `anyrt deploy` would refuse:\n  {}",
        refused.join("\n  ")
    );
}
