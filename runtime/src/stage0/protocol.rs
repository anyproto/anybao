//! Strict JSON request parsing and unwind containment for the Stage-0 JNI seam.
use super::harness::PROTOCOL;
use anyhow::{ensure, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Load {},
    PrepareIdle {},
    K2Case {
        index: u32,
    },
    PythonRecursionDiagnostic {},
    Turn {
        request: Box<crate::stage0_fixture::TurnRequest>,
    },
    Health {},
    Retire {},
}

pub fn parse_request(text: &str) -> Result<Operation> {
    ensure!(text.len() <= 4 * 1024 * 1024, "oversized Stage-0 request");
    let mut request: serde_json::Map<String, Value> =
        serde_json::from_str(text).map_err(|_| anyhow::anyhow!("invalid Stage-0 request"))?;
    ensure!(
        request.remove("protocol") == Some(json!(PROTOCOL)),
        "JNI protocol mismatch"
    );
    // Never print serde's source-value diagnostics: the input can contain a key.
    serde_json::from_value(Value::Object(request))
        .map_err(|_| anyhow::anyhow!("invalid Stage-0 operation"))
}

pub fn response(terminal: &AtomicBool, run: impl FnOnce() -> Result<Value>) -> String {
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(run));
    let mut native_failure = None;
    let result = match result {
        Ok(Err(error)) if error.downcast_ref::<super::RunThreadPanic>().is_some() => {
            terminal.store(true, Ordering::Relaxed);
            Err("native run thread panicked; retire worker".to_string())
        }
        Ok(result) => result.map_err(|error| {
            native_failure = Some(super::NativeFailure::from_anyhow(&error));
            format!("{error:#}")
        }),
        Err(_) => {
            terminal.store(true, Ordering::Relaxed);
            Err("native JNI panic; retire worker".to_string())
        }
    };
    match result {
        Ok(value) => {
            json!({"protocol": PROTOCOL, "ok": true, "terminal_panic": false, "result": value})
        }
        Err(error) => json!({"protocol": PROTOCOL, "ok": false,
            "terminal_panic": terminal.load(Ordering::Relaxed), "error": error,
            "native_failure": native_failure}),
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_versioned_fixed_operations_are_accepted() {
        assert!(matches!(
            parse_request(r#"{"protocol":1,"op":"load"}"#).unwrap(),
            Operation::Load {}
        ));
        for input in [
            r#"{"protocol":2,"op":"load"}"#,
            r#"{"op":"load"}"#,
            r#"{"protocol":1,"op":"load","artifact":"/tmp/evil"}"#,
            r#"{"protocol":1,"op":"compile"}"#,
            r#"{"protocol":1,"op":"k2_case","index":"secret-key"}"#,
        ] {
            let error = parse_request(input).err().unwrap().to_string();
            assert!(!error.contains("secret-key"));
        }
        assert!(matches!(
            parse_request(r#"{"protocol":1,"op":"k2_case","index":199}"#).unwrap(),
            Operation::K2Case { index: 199 }
        ));
    }

    #[test]
    fn both_jni_and_joined_execution_panics_require_retirement() {
        let terminal = AtomicBool::new(false);
        let reply: Value =
            serde_json::from_str(&response(&terminal, || panic!("test panic"))).unwrap();
        assert_eq!(reply["terminal_panic"], true);
        assert_eq!(reply["ok"], false);
        let terminal = AtomicBool::new(false);
        let reply: Value = serde_json::from_str(&response(&terminal, || {
            super::super::on_run_thread(|| panic!("run panic"))
        }))
        .unwrap();
        assert_eq!(reply["terminal_panic"], true);
        assert_eq!(reply["ok"], false);
    }

    #[test]
    fn an_initialization_trap_keeps_its_raw_cause_in_the_error_envelope() {
        let terminal = AtomicBool::new(false);
        let reply: Value = serde_json::from_str(&response(&terminal, || {
            Err(wasmtime::Error::new(wasmtime::Trap::MemoryOutOfBounds).into())
        }))
        .unwrap();
        assert_eq!(reply["ok"], false);
        assert_eq!(reply["terminal_panic"], false);
        assert_eq!(reply["native_failure"]["cause"], "MemoryOutOfBounds");
    }
}
