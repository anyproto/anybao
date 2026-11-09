//! `managed_credentials` in `resolve_static` (N2a-r2 isolation NEW-7): on the
//! managed path a guest-supplied `about.hosts` is never a binding and an open
//! row is never bound on first use; the desktop path keeps the ADR-032 §3
//! first-use bind. Calls `resolve_static` directly: no HTTP leaves the test.
use super::*;

/// Rows by key, and every store call the broker makes, in order.
#[derive(Default)]
struct Rows {
    rows: BTreeMap<String, Value>,
    calls: Mutex<Vec<String>>,
}
impl Rows {
    fn with(rows: &[(&str, Value)]) -> Arc<Self> {
        Arc::new(Rows {
            rows: rows
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect(),
            calls: Mutex::default(),
        })
    }
    fn log(&self, call: String) {
        self.calls.lock().unwrap().push(call);
    }
    fn writes(&self) -> Vec<String> {
        self.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|c| !c.starts_with("read"))
            .cloned()
            .collect()
    }
}
impl SecretSource for Rows {
    fn read(&self, key: &str) -> Result<Option<String>, String> {
        self.log(format!("read {key}"));
        Ok(self
            .rows
            .get(key)
            .and_then(|r| r["value"].as_str().map(str::to_owned)))
    }
    fn mark_missing(&self, key: &str, _about: &Value, _run_id: &str) {
        self.log(format!("missing {key}"));
    }
    fn mark_rejected(&self, key: &str, _about: &Value, _run_id: &str, status: u16) {
        self.log(format!("rejected {key} {status}"));
    }
    fn read_row(&self, key: &str) -> Result<Option<Value>, String> {
        self.log(format!("read_row {key}"));
        Ok(self.rows.get(key).cloned())
    }
    fn mark_used(&self, key: &str) {
        self.log(format!("used {key}"));
    }
    fn bind_hosts(&self, key: &str, _about: &Value) {
        self.log(format!("bind {key}"));
    }
}

struct Declared(BTreeMap<String, Value>);
impl DeclaredCredentials for Declared {
    fn about(&self, key: &str) -> Option<Value> {
        self.0.get(key).cloned()
    }
}

fn broker(managed: bool, rows: &Arc<Rows>, declared: &[(&str, Value)]) -> Broker {
    let mut b = Broker::new(
        TraceWriter::new(json!({"id": "run_new7"})),
        BTreeMap::new(),
        BTreeMap::new(),
        None,
        Classifier::new(None),
    );
    b.secret_store = Some(rows.clone() as Arc<dyn SecretSource>);
    b.declared = Some(Arc::new(Declared(
        declared
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect(),
    )));
    b.managed_credentials = managed;
    b
}

fn guest_about(host: &str) -> Value {
    json!({"label": "mine", "hosts": [host]})
}

#[test]
fn a_hostless_open_ref_is_refused_on_the_managed_path_whatever_the_guest_says() {
    // a valued row without hosts, and no row at all
    let rows = Rows::with(&[("local.key.x", json!({"key": "local.key.x", "value": "sk"}))]);
    let mut b = broker(true, &rows, &[]);
    for r in ["local.key.x", "local.key.absent"] {
        let error = b
            .resolve_static(r, Some(&guest_about("evil.test")), "https://evil.test/a")
            .unwrap_err();
        assert_eq!(error.type_, "secret_hosts_required", "{r}");
    }
    assert!(
        rows.writes().is_empty(),
        "no miss stamp, no bind, no use stamp: {:?}",
        rows.writes()
    );
    assert!(b.missing_secrets.is_empty());
}

#[test]
fn a_declared_hostless_ref_falls_back_to_its_rows_hosts() {
    let rows = Rows::with(&[(
        "llm.key.selfhosted",
        json!({"key": "llm.key.selfhosted", "value": "sk", "hosts": ["llm.example"]}),
    )]);
    let declared = [("llm.key.selfhosted", json!({"label": "Self-hosted"}))];
    let mut b = broker(true, &rows, &declared);
    let error = b
        .resolve_static(
            "llm.key.selfhosted",
            Some(&guest_about("evil.test")),
            "https://evil.test/v1",
        )
        .unwrap_err();
    assert_eq!(error.type_, "secret_host_mismatch");
    let (value, descriptor) = b
        .resolve_static(
            "llm.key.selfhosted",
            Some(&guest_about("evil.test")),
            "https://llm.example/v1",
        )
        .unwrap();
    assert_eq!(value, "sk");
    assert_eq!(
        descriptor["label"], "Self-hosted",
        "the declaration, not the guest's"
    );
    assert_eq!(rows.writes(), vec!["used llm.key.selfhosted"]);
}

#[test]
fn a_valued_open_row_goes_only_to_its_own_hosts_and_is_never_bound() {
    let rows = Rows::with(&[(
        "local.key.x",
        json!({"key": "local.key.x", "value": "sk", "hosts": ["api.x.test"]}),
    )]);
    let mut b = broker(true, &rows, &[]);
    let error = b
        .resolve_static(
            "local.key.x",
            Some(&guest_about("evil.test")),
            "https://evil.test/a",
        )
        .unwrap_err();
    assert_eq!(error.type_, "secret_host_mismatch");
    let (value, _) = b
        .resolve_static(
            "local.key.x",
            Some(&guest_about("evil.test")),
            "https://api.x.test/a",
        )
        .unwrap();
    assert_eq!(value, "sk");
    assert_eq!(rows.writes(), vec!["used local.key.x"], "no bind");
}

#[test]
fn off_the_managed_path_an_open_row_still_binds_on_first_use() {
    let rows = Rows::with(&[("local.key.x", json!({"key": "local.key.x", "value": "sk"}))]);
    let mut b = broker(false, &rows, &[]);
    let (value, _) = b
        .resolve_static(
            "local.key.x",
            Some(&guest_about("api.x.test")),
            "https://api.x.test/a",
        )
        .unwrap();
    assert_eq!(value, "sk");
    assert_eq!(
        rows.writes(),
        vec!["bind local.key.x", "used local.key.x"],
        "ADR-032 §3: the first use freezes where the key goes"
    );
}
