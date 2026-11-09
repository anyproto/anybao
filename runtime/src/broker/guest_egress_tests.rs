use super::*;
use crate::guest_egress::AndroidPocEgress;
use std::net::{SocketAddr, TcpListener};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

fn broker(origin: &str) -> Broker {
    let mut broker = Broker::new(
        TraceWriter::new(json!({"id": "run_egress"})),
        BTreeMap::new(),
        BTreeMap::new(),
        None,
        Classifier::new(Some(origin)),
    );
    broker.set_guest_egress(AndroidPocEgress::new(origin).unwrap());
    broker
}

fn server() -> (tiny_http::Server, String) {
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", server.server_addr());
    (server, origin)
}

fn second_listener() -> (TcpListener, SocketAddr) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    (listener, address)
}

fn no_connection(listener: &TcpListener) {
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[test]
fn guest_egress_data_reaches_only_the_leased_listener_and_denials_are_traced() {
    let (allowed, origin) = server();
    let (other, other_address) = second_listener();
    let mut broker = broker(&origin);
    for url in [
        format!("http://{other_address}/v1/account"),
        format!("http://[::ffff:7f00:1]:{}/v1/account", other_address.port()),
        format!("{origin}/v1/auth"),
        format!("{origin}/v1/local/export"),
        format!("{origin}/v1/spaces/s/debug"),
    ] {
        assert_eq!(
            broker
                .call("http.get", json!({"url":url}))
                .unwrap_err()
                .type_,
            "egress_denied"
        );
        assert_eq!(
            broker.writer.records.last().unwrap()["error"]["type"],
            "egress_denied"
        );
    }
    for (method, path) in [
        ("put", "/v1/devices/me"),
        ("post", "/v1/devices/activate"),
        ("delete", "/v1/devices/victim"),
        ("post", "/v1/local/import"),
        ("connect", "/v1/account"),
        ("trace", "/v1/account"),
    ] {
        assert_eq!(
            broker
                .call(
                    &format!("http.{method}"),
                    json!({"url":format!("{origin}{path}"), "json": {}})
                )
                .unwrap_err()
                .type_,
            "egress_denied"
        );
    }
    assert!(allowed
        .recv_timeout(Duration::from_millis(20))
        .unwrap()
        .is_none());
    no_connection(&other);
    let handle = std::thread::spawn(move || {
        let mut req = allowed
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap();
        assert_eq!(req.method().as_str(), "POST");
        assert_eq!(req.url(), "/v1/spaces/s/modify");
        let mut body = String::new();
        req.as_reader().read_to_string(&mut body).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&body).unwrap(),
            json!({"records": []})
        );
        req.respond(tiny_http::Response::from_string("ok")).unwrap();
    });
    let out = broker
        .call(
            "http.post",
            json!({"url":format!("{origin}/v1/spaces/s/modify"), "json":{"records":[]}}),
        )
        .unwrap();
    assert_eq!(out["body"], "ok");
    handle.join().unwrap();
    no_connection(&other);
}

#[test]
fn guest_egress_redirects_recheck_path_port_encoding_and_ipv4_mapped_ipv6() {
    for status in [301, 302, 303, 307, 308] {
        for target_kind in ["port", "mapped", "registry", "path", "encoded"] {
            let (allowed, origin) = server();
            let (other, other_address) = second_listener();
            let target = match target_kind {
                "port" => format!("http://{other_address}/v1/account"),
                "mapped" => format!("http://[::ffff:7f00:1]:{}/v1/account", other_address.port()),
                "registry" => format!("{origin}/v1/devices/activate"),
                "path" => format!("{origin}/v1/auth"),
                _ => format!("{origin}/v1/spaces/s/../s/query"),
            };
            let handle = std::thread::spawn(move || {
                let req = allowed
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap()
                    .unwrap();
                req.respond(
                    tiny_http::Response::empty(status)
                        .with_header(tiny_http::Header::from_bytes("Location", target).unwrap()),
                )
                .unwrap();
                assert!(allowed
                    .recv_timeout(Duration::from_millis(20))
                    .unwrap()
                    .is_none());
            });
            let mut broker = broker(&origin);
            let result = broker.call(
                "http.post",
                json!({"url":format!("{origin}/v1/spaces/s/query"), "json":{}}),
            );
            assert_eq!(
                result.unwrap_err().type_,
                "egress_denied",
                "{status} {target_kind}"
            );
            handle.join().unwrap();
            no_connection(&other);
        }
    }
}

#[test]
fn guest_egress_permitted_redirect_rechecks_the_changed_method() {
    let (allowed, origin) = server();
    let handle = std::thread::spawn(move || {
        let first = allowed
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap();
        assert_eq!(first.method().as_str(), "POST");
        first
            .respond(
                tiny_http::Response::empty(303)
                    .with_header(tiny_http::Header::from_bytes("Location", "/v1/devices").unwrap()),
            )
            .unwrap();
        let mut second = allowed
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap();
        assert_eq!(second.method().as_str(), "GET");
        assert_eq!(second.url(), "/v1/devices");
        let mut body = String::new();
        second.as_reader().read_to_string(&mut body).unwrap();
        assert!(body.is_empty());
        second
            .respond(tiny_http::Response::from_string("read-only devices"))
            .unwrap();
    });
    let out = broker(&origin)
        .call(
            "http.post",
            json!({"url":format!("{origin}/v1/spaces/s/query"),"json":{}}),
        )
        .unwrap();
    assert_eq!(out["body"], "read-only devices");
    handle.join().unwrap();
}

#[test]
fn guest_egress_dns_alias_and_redirect_dns_never_open_a_second_socket() {
    for redirect in [false, true] {
        let (allowed, origin) = server();
        let (other, other_address) = second_listener();
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = calls.clone();
        let policy = AndroidPocEgress::new(&origin)
            .unwrap()
            .with_resolver(move |_: &str| {
                seen.fetch_add(1, Ordering::SeqCst);
                Ok(vec![other_address])
            });
        let url = format!(
            "https://alias.example.com:{}/v1/account",
            other_address.port()
        );
        let mut broker = broker(&origin);
        broker.set_guest_egress(policy);
        let target = if redirect {
            let target = url.clone();
            let handle = std::thread::spawn(move || {
                let req = allowed
                    .recv_timeout(Duration::from_secs(2))
                    .unwrap()
                    .unwrap();
                req.respond(
                    tiny_http::Response::empty(307)
                        .with_header(tiny_http::Header::from_bytes("Location", target).unwrap()),
                )
                .unwrap();
            });
            Some(handle)
        } else {
            None
        };
        let input = if redirect {
            format!("{origin}/v1/account")
        } else {
            url
        };
        assert_eq!(
            broker
                .call("http.get", json!({"url":input}))
                .unwrap_err()
                .type_,
            "egress_denied"
        );
        if let Some(handle) = target {
            handle.join().unwrap();
        }
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        no_connection(&other);
    }
}

struct UnreadSecrets;
impl SecretSource for UnreadSecrets {
    fn read(&self, _: &str) -> Result<Option<String>, String> {
        panic!("credential read before egress/replay gate");
    }
    fn mark_missing(&self, _: &str, _: &Value, _: &str) {
        panic!("credential write before egress/replay gate");
    }
    fn mark_rejected(&self, _: &str, _: &Value, _: &str, _: u16) {
        panic!("credential write before egress/replay gate");
    }
}

#[test]
fn guest_egress_denies_before_credentials_and_blocks_header_smuggling() {
    let (allowed, origin) = server();
    let mut broker = broker(&origin);
    broker.secret_store = Some(Arc::new(UnreadSecrets));
    for (path, headers) in [
        ("/v1/auth", json!({})),
        ("/v1/account", json!({"Host":"other.example.com"})),
        ("/v1/account", json!({"Content-Length":"1"})),
        ("/v1/account", json!({"Transfer-Encoding":"chunked"})),
        ("/v1/account", json!({"X-HTTP-Method-Override":"DELETE"})),
    ] {
        let out = broker.call(
            "http.get",
            json!({"url":format!("{origin}{path}"),"headers":headers,
            "credential":{"ref":"llm.key.test","header":"Authorization"}}),
        );
        assert_eq!(out.unwrap_err().type_, "egress_denied");
    }
    assert_eq!(
        broker
            .call(
                "http.get",
                json!({"url":format!("{origin}/v1/account"),
        "credential":{"ref":"llm.key.test","header":"Host"}})
            )
            .unwrap_err()
            .type_,
        "egress_denied"
    );
    for reference in ["connector.oauth.google", "connector.oauth.openai-codex"] {
        assert_eq!(
            broker
                .call(
                    "http.get",
                    json!({"url":"https://api.example.com/", "credential":{"ref":reference}})
                )
                .unwrap_err()
                .type_,
            "UnsupportedCapability"
        );
    }
    for name in ["oauth.connect", "oauth.refresh", "oauth.status", "sh.run"] {
        assert_eq!(
            broker.call(name, json!({})).unwrap_err().type_,
            "UnsupportedCapability"
        );
    }
    assert!(allowed
        .recv_timeout(Duration::from_millis(20))
        .unwrap()
        .is_none());
}

#[test]
fn guest_egress_replay_has_no_dns_credential_or_network_side_effect() {
    let (other, other_address) = second_listener();
    let origin = format!("http://{other_address}");
    for url in [
        format!("https://offline.example.com:{}/mcp", other_address.port()),
        format!("{origin}/v1/auth"),
    ] {
        let payload =
            json!({"url":url, "credential":{"ref":"llm.key.test","header":"Authorization"}});
        let mut trace = TraceWriter::new(json!({"id":"recorded"}));
        trace.effect(
            "http.get",
            None,
            payload.clone(),
            &input_key("http.get", &payload),
            Some(json!({"status":200,"body":"offline"})),
            None,
            json!({"class":"read"}),
            None,
        );
        let mut broker = broker(&origin);
        broker.set_guest_egress(AndroidPocEgress::new(&origin).unwrap().with_resolver(
            |_: &str| {
                panic!("DNS during replay");
            },
        ));
        broker.secret_store = Some(Arc::new(UnreadSecrets));
        broker.mode = Mode::Replay;
        broker.cursor = Some(ReplayCursor::new(&trace.records));
        let out = broker.call("http.get", payload).unwrap();
        assert_eq!(out["body"], "offline");
        assert_eq!(
            broker.writer.records.last().unwrap()["meta"]["mocked"],
            true
        );
        no_connection(&other);
    }
}

#[test]
fn guest_egress_batch_uses_the_same_boundary() {
    let (other, address) = second_listener();
    let mut broker = broker("http://127.0.0.1:7011");
    let output = broker
        .call(
            "batch",
            json!({"name":"http.get", "payloads":[
        {"url":format!("http://{address}/v1/account")}, {"url":"http://127.0.0.1:7011/v1/auth"}]}),
        )
        .unwrap();
    for item in output["results"].as_array().unwrap() {
        assert_eq!(item["error"]["type"], "egress_denied");
    }
    no_connection(&other);
}

#[test]
fn guest_egress_desktop_default_still_reaches_other_local_origins() {
    let (server, origin) = server();
    let handle = std::thread::spawn(move || {
        server
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap()
            .respond(tiny_http::Response::from_string("desktop default"))
            .unwrap();
    });
    let mut broker = Broker::new(
        TraceWriter::new(json!({"id":"desktop"})),
        BTreeMap::new(),
        BTreeMap::new(),
        None,
        Classifier::new(None),
    );
    let out = broker
        .call("http.get", json!({"url":format!("{origin}/arbitrary")}))
        .unwrap();
    assert_eq!(out["body"], "desktop default");
    handle.join().unwrap();
}

#[test]
fn guest_egress_ipv6_literal_lease_connects_without_dns() {
    let server = tiny_http::Server::http("[::1]:0").unwrap();
    let origin = format!("http://{}", server.server_addr());
    let handle = std::thread::spawn(move || {
        server
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap()
            .respond(tiny_http::Response::from_string("IPv6 lease"))
            .unwrap();
    });
    let mut broker = broker(&origin);
    broker.set_guest_egress(
        AndroidPocEgress::new(&origin)
            .unwrap()
            .with_resolver(|_: &str| {
                panic!("literal IPv6 lease must not resolve DNS");
            }),
    );
    let out = broker
        .call("http.get", json!({"url":format!("{origin}/v1/account")}))
        .unwrap();
    assert_eq!(out["body"], "IPv6 lease");
    handle.join().unwrap();
}
