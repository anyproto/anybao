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
pub mod broker;
pub mod caps;
pub mod config;
pub mod deploy;
pub mod drift;
pub mod kernelcache;
pub mod replay;
pub mod resolver;
pub mod routes;
pub mod runner;
pub mod serve;
pub mod stats;
#[cfg(test)]
pub mod testutil;
pub mod toolmd;
pub mod trace;
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
