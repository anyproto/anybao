use super::*;
use crate::anyapi::Transport;
use crate::local_grant::{
    Grant, GrantEnvelope, GrantLockDirectory, GrantSlot, GrantState, GrantStoreError,
    LocalGrantStore, Principal, Secret,
};
use base64::Engine;
use std::sync::atomic::AtomicUsize;
use std::sync::{mpsc, Condvar};

struct SyntheticCustody {
    envelope: GrantEnvelope,
    reads: AtomicUsize,
}
impl LocalGrantStore for SyntheticCustody {
    fn namespace(&self) -> &str {
        "bao.readiness.synthetic"
    }
    fn read(&self, _: &GrantSlot) -> Result<Option<GrantEnvelope>, GrantStoreError> {
        self.reads.fetch_add(1, Ordering::Relaxed);
        Ok(Some(self.envelope.clone()))
    }
    fn replace(&self, _: &GrantSlot, _: &GrantEnvelope) -> Result<(), GrantStoreError> {
        panic!("readiness must not mutate custody")
    }
}

fn auth(
    profile: &str,
    generation: u64,
) -> (
    tempfile::TempDir,
    Arc<SyntheticCustody>,
    Arc<crate::chatgpt::ChatGptAuth>,
) {
    let dir = tempfile::tempdir().unwrap();
    let slot = GrantSlot::new("bao/test", profile, "any-account", "peer").unwrap();
    let expiry = now_s() as u64 + 3600;
    let claims = json!({"iss":"https://auth.openai.com", "sub":"synthetic-subject", "exp":expiry,
        "https://api.openai.com/auth":{"chatgpt_account_id":"synthetic-account"}});
    let token = format!(
        "e30.{}.synthetic-signature",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(claims.to_string())
    );
    let mut envelope = GrantEnvelope::empty(&slot);
    envelope.revision = 1;
    envelope.grant = Some(Grant {
        id: uuid::Uuid::new_v4().to_string(),
        pair_revision: 1,
        access: Secret(token),
        refresh: Secret("synthetic-refresh".into()),
        expires_at: expiry,
        principal: Principal {
            issuer: "https://auth.openai.com".into(),
            subject: "synthetic-subject".into(),
            account_id: "synthetic-account".into(),
        },
        account_label: Some("private-label".into()),
        plan_type: None,
        residency: None,
        state: GrantState::Ready,
        retry_after: None,
    });
    let custody = Arc::new(SyntheticCustody {
        envelope,
        reads: AtomicUsize::new(0),
    });
    let service = crate::chatgpt::ChatGptAuth::new(
        slot,
        generation,
        custody.clone(),
        GrantLockDirectory::new(dir.path().join("locks")).unwrap(),
        None,
    )
    .unwrap();
    (dir, custody, service)
}

struct ConfigStall {
    records: Value,
    events: mpsc::Sender<Value>,
    entered: mpsc::Sender<()>,
    finished: mpsc::Sender<()>,
    release: Arc<(Mutex<bool>, Condvar)>,
    queries: Arc<AtomicUsize>,
}
impl Transport for ConfigStall {
    fn send(&self, _: &str, path: &str, body: Option<&Value>) -> Result<(u16, Value), AnyError> {
        if path.ends_with("/query") {
            let call = self.queries.fetch_add(1, Ordering::Relaxed);
            if path.contains("/session-A/") && call > 0 {
                self.entered.send(()).unwrap();
                let (lock, changed) = &*self.release;
                let guard = lock.lock().unwrap();
                let (guard, timeout) = changed
                    .wait_timeout_while(guard, Duration::from_secs(30), |v| !*v)
                    .unwrap();
                assert!(
                    *guard && !timeout.timed_out(),
                    "test must release its blocked query"
                );
                self.finished.send(()).unwrap();
            }
            return Ok((200, json!({"records":self.records})));
        }
        assert_eq!(path, "/v1/events");
        self.events.send(body.unwrap().clone()).unwrap();
        Ok((200, json!({})))
    }
    fn open_stream(
        &self,
        _: &str,
        _: Option<&Value>,
    ) -> Result<Box<dyn Iterator<Item = String>>, AnyError> {
        panic!("readiness never opens a stream")
    }
}

fn wait_event(rx: &mpsc::Receiver<Value>, predicate: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + Duration::from_secs(14);
    loop {
        let event = rx
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .unwrap();
        if predicate(&event) {
            return event;
        }
    }
}

#[test]
fn stalled_projection_keeps_real_publisher_alive_expires_ready_and_fences_old_session() {
    let (event_tx, events) = mpsc::channel();
    let (entered_tx, entered) = mpsc::channel();
    let (finished_tx, finished) = mpsc::channel();
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    let queries = Arc::new(AtomicUsize::new(0));
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../docs/fixtures/chatgpt-model-config-v1.json"
    ))
    .unwrap();
    let client = Arc::new(Client::with_transport(Box::new(ConfigStall {
        records: fixture["snapshots"][0]["rows"].clone(),
        events: event_tx,
        entered: entered_tx,
        finished: finished_tx,
        release: release.clone(),
        queries: queries.clone(),
    })));
    let (_dir_a, custody_a, auth_a) = auth("session-A", 1);
    let source = ModelReadinessSource {
        generation: 1,
        scope: "a".repeat(64),
        config: Some(Arc::new(ServeConfigStore::new(
            client.clone(),
            "session-A",
            "obj",
            "config",
        ))),
        auth: Some(auth_a.clone()),
    };
    let stop_a = Arc::new(AtomicBool::new(false));
    let status = Arc::new(PresenceState::default());
    let publisher_a = spawn_presence_publisher(
        client.clone(),
        "peer".into(),
        status.clone(),
        stop_a.clone(),
        Some(ReadinessProbe::spawn(source, stop_a.clone())),
        || {
            (
                BeatRole {
                    responder: true,
                    winner: None,
                },
                None,
                None,
            )
        },
    );
    let ready = wait_event(&events, |e| {
        e["data"]["chatgpt"]["configuration"] == "matched"
    });
    assert_eq!(ready["data"]["chatgpt"]["provider"]["state"], "connected");
    entered.recv_timeout(Duration::from_secs(3)).unwrap();
    let unavailable = wait_event(&events, |e| {
        e["data"]["chatgpt"]["configuration"] == "unknown"
    });
    assert_eq!(
        unavailable["data"]["chatgpt"]["provider"]["state"],
        "unavailable"
    );
    assert_eq!(
        unavailable["data"]["chatgpt"]["publisherScope"],
        "a".repeat(64)
    );
    // The next ordinary cadence beat still arrives while ONLY the query is held.
    let heartbeat = events.recv_timeout(Duration::from_secs(13)).unwrap();
    assert_eq!(heartbeat["data"]["chatgpt"]["configuration"], "unknown");
    assert_eq!(queries.load(Ordering::Relaxed), 2); // one outstanding query, no fan-out
    let stopping = Instant::now();
    stop_a.store(true, Ordering::Release);
    publisher_a.join().unwrap();
    assert!(stopping.elapsed() < Duration::from_secs(1));
    assert_eq!(
        events.recv_timeout(Duration::from_secs(1)).unwrap()["data"]["state"],
        "shutdown"
    );
    auth_a.shutdown();
    let reads_at_stop = custody_a.reads.load(Ordering::Relaxed);

    let (_dir_b, _custody_b, auth_b) = auth("session-B", 2);
    let source_b = ModelReadinessSource {
        generation: 2,
        scope: "b".repeat(64),
        config: Some(Arc::new(ServeConfigStore::new(
            client.clone(),
            "session-B",
            "obj",
            "config",
        ))),
        auth: Some(auth_b.clone()),
    };
    let stop_b = Arc::new(AtomicBool::new(false));
    let publisher_b = spawn_presence_publisher(
        client,
        "peer".into(),
        status,
        stop_b.clone(),
        Some(ReadinessProbe::spawn(source_b, stop_b.clone())),
        || {
            (
                BeatRole {
                    responder: true,
                    winner: None,
                },
                None,
                None,
            )
        },
    );
    *release.0.lock().unwrap() = true;
    release.1.notify_all();
    finished.recv_timeout(Duration::from_secs(1)).unwrap();
    let fresh = wait_event(&events, |event| {
        assert_eq!(event["data"]["chatgpt"]["bootGeneration"], "2");
        assert_eq!(event["data"]["chatgpt"]["publisherScope"], "b".repeat(64));
        event["data"]["chatgpt"]["provider"]["state"] == "connected"
    });
    assert_eq!(custody_a.reads.load(Ordering::Relaxed), reads_at_stop);
    assert!(!fresh.to_string().contains("private-label"));
    stop_b.store(true, Ordering::Release);
    publisher_b.join().unwrap();
    auth_b.shutdown();
    assert_eq!(
        events.recv_timeout(Duration::from_secs(1)).unwrap()["data"]["chatgpt"]["bootGeneration"],
        "2"
    );
    assert!(events.try_recv().is_err());
}

#[test]
fn projection_http_budget_is_separate_from_the_ordinary_client() {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let addr = format!("http://{}", server.server_addr());
    let (release, wait) = mpsc::channel();
    let server_thread = std::thread::spawn(move || {
        let request = server.recv().unwrap();
        assert!(request.url().ends_with("/query"));
        wait.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(request);
    });
    let ordinary = Arc::new(Client::with_transport(Box::new(
        crate::testutil::StubTransport::new(),
    )));
    let mut store = ServeConfigStore::new(ordinary.clone(), "space", "obj", "config");
    store.projection_client = Arc::new(Client::with_transport(Box::new(
        crate::anyapi::HttpTransport::with_timeouts(
            &addr,
            READINESS_HTTP_BUDGET,
            READINESS_HTTP_BUDGET,
        ),
    )));
    let started = Instant::now();
    assert!(store.model_configuration().is_none());
    assert!(started.elapsed() < Duration::from_secs(2));
    ordinary.publish_event(&json!({"synthetic":true})).unwrap();
    release.send(()).unwrap();
    server_thread.join().unwrap();
}
