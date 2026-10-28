use super::*;
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::thread;

const WINDOW: &str = "d235081b-d19a-4d48-a5f8-c2e3bb6bcf33";
const OTHER: &str = "20667448-0584-4996-b6bf-cb00c0a578b1";

fn incoming(listener: &TcpListener) -> (TcpStream, String, Value) {
    listener.set_nonblocking(true).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let (mut stream, _) = loop {
        match listener.accept() {
            Ok(connection) => break connection,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(1));
            }
            Err(e) => panic!("Expected a request before the fixture deadline: {e}"),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut reader = BufReader::new(&mut stream);
    let mut head = String::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
        assert!(!line.is_empty());
        head.push_str(&line);
    }
    let len = head
        .lines()
        .find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    let mut body = vec![0; len];
    reader.read_exact(&mut body).unwrap();
    let body = if body.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&body).unwrap()
    };
    drop(reader);
    (stream, head, body)
}

fn reply(stream: &mut TcpStream, subscribers: u64) {
    let body = json!({"subscribers": subscribers}).to_string();
    write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
}

fn result(id: &str, window: &str, ok: bool) -> Value {
    let mut data = json!({"version": 1, "requestId": id, "windowId": window,
        "completedAt": 1, "state": {"selectedId": "any", "preview": null}, "ok": ok});
    if ok {
        data["result"] = json!({"themeId": "mint"});
    } else {
        data["error"] = json!({"code": "disabled", "message": "AI-free mode"});
    }
    json!({"type": "ui.theme.result", "scope": "device", "target": id,
        "sender": {"self": true}, "data": data})
}

fn emit(stream: &mut TcpStream, value: &Value) {
    write!(stream, "event: event\ndata: {value}\n\n").unwrap();
}

fn fixture<T: Send + 'static>(
    f: impl FnOnce(TcpStream, Value) -> T + Send + 'static,
) -> (String, thread::JoinHandle<T>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        let (mut stream, head, _) = incoming(&listener);
        assert!(
            head.starts_with("GET /v1/events/subscribe?scope=device&type=ui.theme.result&target=")
        );
        stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: ready\ndata: {}\n\n").unwrap();
        let (mut publish, post_head, body) = incoming(&listener);
        assert!(post_head.starts_with("POST /v1/events HTTP/1.1"));
        assert_eq!(body["type"], "ui.theme.request");
        assert_eq!(body["scope"], "device");
        assert!(head.contains(body["data"]["requestId"].as_str().unwrap()));
        assert_eq!(body["target"], body["data"]["requestId"]);
        reply(&mut publish, 1);
        f(stream, body)
    });
    (base, handle)
}

#[test]
fn subscribes_before_publish_and_returns_only_the_matching_ui_reply() {
    for ok in [true, false] {
        let (base, server) = fixture(move |mut stream, body| {
            assert_eq!(body["data"]["action"], "select");
            assert_eq!(body["data"]["params"], json!({"themeId": "mint"}));
            assert_eq!(body["data"]["windowId"], WINDOW);
            assert_eq!(body["data"]["version"], 1);
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as u64;
            assert!(body["data"]["expiresAt"].as_u64().unwrap() <= now + 1000);
            let id = body["target"].as_str().unwrap();
            emit(&mut stream, &result("unrelated", WINDOW, ok));
            emit(&mut stream, &result(id, OTHER, ok));
            let mut foreign = result(id, WINDOW, ok);
            foreign["sender"]["self"] = json!(false);
            emit(&mut stream, &foreign);
            foreign["sender"]["self"] = json!(true);
            foreign["scope"] = json!("account");
            emit(&mut stream, &foreign);
            foreign["scope"] = json!("device");
            foreign["data"]["version"] = json!(2);
            emit(&mut stream, &foreign);
            let wanted = result(id, WINDOW, ok);
            emit(&mut stream, &wanted);
            wanted["data"].clone()
        });
        let out = request(
            &base,
            &json!({"action": "select", "windowId": WINDOW,
            "params": {"themeId": "mint"}, "timeout": 1}),
            None,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(out, server.join().unwrap());
    }
}

#[test]
fn discovery_collects_windows_and_finishes_despite_keepalives() {
    let (base, server) = fixture(|mut stream, body| {
        assert!(body["data"].get("windowId").is_none());
        let id = body["target"].as_str().unwrap();
        emit(&mut stream, &result(id, WINDOW, true));
        emit(&mut stream, &result(id, OTHER, false));
        emit(&mut stream, &result(id, WINDOW, true));
        for _ in 0..100 {
            if stream.write_all(b": keepalive\n\n").is_err() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
    });
    let start = Instant::now();
    let out = request(
        &base,
        &json!({"action": "capabilities", "timeout": 0.2}),
        None,
        &AtomicBool::new(false),
    )
    .unwrap();
    assert!(start.elapsed() < Duration::from_millis(800));
    assert_eq!(out["windows"].as_array().unwrap().len(), 2);
    assert_eq!(out["windows"][0]["windowId"], OTHER);
    server.join().unwrap();
}

#[test]
fn missing_result_has_an_absolute_run_bound_and_no_automatic_retry() {
    let (base, server) = fixture(|mut stream, _| {
        for _ in 0..100 {
            if stream.write_all(b": keepalive\n\n").is_err() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
    });
    let start = Instant::now();
    let out = request(
        &base,
        &json!({"action": "select", "windowId": WINDOW,
        "params": {"themeId": "mint"}, "timeout": 30}),
        Some(start + Duration::from_millis(150)),
        &AtomicBool::new(false),
    )
    .unwrap();
    assert!(start.elapsed() < Duration::from_millis(800));
    assert_eq!(out["ok"], false);
    assert_eq!(out["error"]["code"], "outcome_unknown");
    assert!(uuid::Uuid::parse_str(out["requestId"].as_str().unwrap()).is_ok());
    server.join().unwrap();
}

#[test]
fn disconnect_after_publish_is_an_unknown_outcome() {
    let (base, server) = fixture(|_, _| {});
    let out = request(
        &base,
        &json!({"action": "export", "windowId": WINDOW}),
        None,
        &AtomicBool::new(false),
    )
    .unwrap();
    assert_eq!(out["error"]["code"], "outcome_unknown");
    server.join().unwrap();
}

#[test]
fn oversized_unterminated_frames_fail_without_unbounded_allocation() {
    let (base, server) = fixture(|mut stream, _| {
        let _ = stream.write_all(&vec![b'x'; FRAME_BYTES + 1]);
    });
    let out = request(
        &base,
        &json!({"action": "get", "windowId": WINDOW}),
        None,
        &AtomicBool::new(false),
    )
    .unwrap();
    assert_eq!(out["error"]["code"], "outcome_unknown");
    assert!(out["error"]["message"]
        .as_str()
        .unwrap()
        .contains("byte limit"));
    server.join().unwrap();
}

#[test]
fn invalid_inputs_and_interruption_cannot_publish() {
    for payload in [
        json!({"action": "select"}),
        json!({"action": "invented"}),
        json!({"action": "capabilities", "timeout": 0}),
        json!({"action": "capabilities", "timeout": 31}),
        json!({"action": "list", "windowId": "bad"}),
        json!({"action": "capabilities", "url": "https://elsewhere"}),
    ] {
        assert_eq!(
            request(
                "http://127.0.0.1:1",
                &payload,
                None,
                &AtomicBool::new(false)
            )
            .unwrap_err()
            .type_,
            "ValueError"
        );
    }
    let out = request(
        "http://127.0.0.1:1",
        &json!({"action": "capabilities"}),
        None,
        &AtomicBool::new(true),
    )
    .unwrap();
    assert_eq!(out["error"]["code"], "unavailable");
    assert!(out["error"]["message"]
        .as_str()
        .unwrap()
        .contains("interrupted"));
}

#[test]
fn zero_consumers_is_unavailable_and_a_lost_publish_reply_is_unknown() {
    for acknowledge in [true, false] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            let (mut stream, _, _) = incoming(&listener);
            stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: ready\ndata: {}\n\n").unwrap();
            let (mut publish, _, _) = incoming(&listener);
            if acknowledge {
                reply(&mut publish, 0);
            }
            // Dropping an unacknowledged POST may follow an applied mutation.
            listener
        });
        let out = request(
            &base,
            &json!({"action": "select", "windowId": WINDOW}),
            None,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(
            out["error"]["code"],
            if acknowledge {
                "unavailable"
            } else {
                "outcome_unknown"
            }
        );
        let listener = server.join().unwrap();
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn an_interrupt_while_waiting_for_ready_prevents_publication() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let interrupted = std::sync::Arc::new(AtomicBool::new(false));
    let signal = interrupted.clone();
    let server = thread::spawn(move || {
        let (mut stream, _, _) = incoming(&listener);
        signal.store(true, Ordering::Relaxed);
        stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: ready\ndata: {}\n\n").unwrap();
        listener
    });
    let out = request(
        &base,
        &json!({"action": "select", "windowId": WINDOW}),
        None,
        &interrupted,
    )
    .unwrap();
    assert_eq!(out["error"]["code"], "unavailable");
    let listener = server.join().unwrap();
    listener.set_nonblocking(true).unwrap();
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
}
