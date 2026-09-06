//! anyrt — the anybao runtime as a library (ADR-009 §6): the wasmtime
//! cage, broker/trace, and the syscall surface (ADR-002). Guest
//! modules (the whole agent) load from program space; the crate
//! carries no product logic. The `anyrt` bin is a thin CLI over this —
//! embedders build a [`Config`] programmatically, then either run the
//! full agent ([`serve::start`] → [`serve::AgentHandle`]), run one
//! program ([`runner::run_program`]), or replay a trace ([`replay`]).
//!
//! Logging rides `tracing`: embedders install a subscriber (or get
//! silence); the bin installs a fmt subscriber.

pub mod anyapi;
pub mod blob;
pub mod broker;
pub mod caps;
pub mod config;
pub mod deploy;
pub mod drift;
pub mod election;
pub mod oauth;
pub mod program_schema;
pub mod replay;
pub mod resolver;
pub mod routes;
pub mod runner;
pub mod serve;
#[cfg(feature = "shell")]
pub mod shell;

/// `runtime.get("shell")` (ADR-024 §4): where bao is — `{cwd, home,
/// shell, os}` in a `--features shell` binary, `null` without. The key
/// always resolves, so the two per-run probes (the kernel's bind, the
/// toolcaller's tool-set decision) are clean reads, never a recorded
/// `KeyError` in every trace.
pub fn shell_runtime_value() -> serde_json::Value {
    #[cfg(feature = "shell")]
    {
        shell::runtime_value()
    }
    #[cfg(not(feature = "shell"))]
    {
        serde_json::Value::Null
    }
}

#[cfg(test)]
mod shell_runtime_value_tests {
    #[test]
    fn shell_key_always_resolves() {
        let v = super::shell_runtime_value();
        if cfg!(feature = "shell") {
            assert_eq!(v["os"], std::env::consts::OS);
        } else {
            assert!(v.is_null());
        }
    }
}
pub mod stats;
#[cfg(test)]
pub mod testutil;
pub mod trace;
pub mod tracestore;
pub mod triggers;
pub mod view;

pub(crate) mod bindings {
    wasmtime::component::bindgen!({
        world: "kernel",
        path: "wit",
    });
}

pub use config::{Config, ConfigBuilder};
pub use runner::{run_program, Cage, RunOutcome};
pub use serve::{start, AgentHandle, RunCtx};
