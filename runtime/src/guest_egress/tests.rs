use super::*;
use std::sync::atomic::{AtomicUsize, Ordering};
use ureq::Resolver as _;

fn policy() -> AndroidPocEgress {
    AndroidPocEgress::new("http://127.0.0.1:7011").unwrap()
}

#[test]
fn lease_requires_a_literal_origin() {
    for bad in [
        "http://localhost:7011",
        "http://127.1:7011",
        "http://2130706433:7011",
        "http://0x7f000001:7011",
        "http://127.0.0.1:7011/v1",
        "http://127.0.0.1:7011?x",
        "http://127.0.0.1:0",
        "https://127.0.0.1:7011",
        "http://[::ffff:7f00:1]:7011",
        "http://user@127.0.0.1:7011",
    ] {
        assert!(AndroidPocEgress::new(bad).is_err(), "{bad}");
    }
    AndroidPocEgress::new("http://[::1]:7011").unwrap();
}

#[test]
fn explicit_method_and_route_table_is_closed() {
    let p = policy();
    for (method, path) in [
        ("GET", "/v1/account"),
        ("GET", "/v1/devices"),
        ("POST", "/v1/devices/query"),
        ("POST", "/v1/spaces/s/query"),
        ("POST", "/v1/spaces/s/modify"),
        ("POST", "/v1/spaces/s/bundles/bao%2Fv1/children"),
        ("GET", "/v1/spaces/s/bundles/system%3Awiki%2Fv1"),
        (
            "PUT",
            "/v1/spaces/s/objects/o/editor/editor_blocks/markdown",
        ),
        ("POST", "/v1/local/query"),
    ] {
        p.check(method, &format!("http://127.0.0.1:7011{path}"))
            .unwrap();
    }
    for (method, path) in [
        ("POST", "/v1/devices/activate"),
        ("PUT", "/v1/devices/me"),
        ("DELETE", "/v1/devices/victim"),
        ("GET", "/v1/auth"),
        ("POST", "/v1/shutdown"),
        ("GET", "/debug/pprof/goroutine"),
        ("GET", "/v1/spaces/s/debug"),
        ("POST", "/v1/local/import"),
        ("GET", "/v1/local/export"),
        ("PUT", "/v1/local-discovery"),
        ("POST", "/v1/push/token"),
        ("POST", "/v1/spaces/s/acl/ownership"),
        ("POST", "/run"),
        ("GET", "/v1/future/query"),
        ("HEAD", "/v1/account"),
        ("CONNECT", "/v1/account"),
        ("POST", "/v1/spaces/s/bundles/bao/v1/children"),
    ] {
        assert!(
            p.check(method, &format!("http://127.0.0.1:7011{path}"))
                .is_err(),
            "{method} {path}"
        );
    }
}

#[test]
fn raw_ambiguity_is_rejected_before_url_normalization() {
    let p = policy();
    for suffix in [
        "/v1/spaces/s/../s/query",
        "/v1/spaces/s/%2E%2E/s/query",
        "/v1/%61ccount",
        "/v1//account",
        "/v1/account/",
        "/v1/account#ignored",
        "/v1\\account",
        "/v1/spaces/s%2Fextra/query",
        "/v1/spaces/s%252Fextra/query",
        "/v1/spaces/s/bundles/bao%2fv1",
        "/v1/spaces/s/bundles/bao%2F..%2Fv1",
        "/v1/spaces/s/bundles/bao%252Fv1",
        "/v1/spaces/s/objects/o%00",
        "/v1/spaces/s/objects/o%FF",
        "/v1/spaces/s/objects/o%",
        "/v1/account\n",
    ] {
        assert!(
            p.check("GET", &format!("http://127.0.0.1:7011{suffix}"))
                .is_err(),
            "{suffix}"
        );
    }
    for url in [
        "http://127.1:7011/v1/account",
        "http://127.0.0.1:7012/v1/account",
        "http://localhost:7011/v1/account",
        "http://[::1]:7011/v1/account",
        "http://[::ffff:7f00:1]:7011/v1/account",
        "http://127.0.0.1:7011@evil.example.com/v1/account",
    ] {
        assert!(p.check("GET", url).is_err(), "{url}");
    }
}

#[test]
fn endpoint_readiness_is_pure_and_requires_all_resolved_addresses_public() {
    for url in [
        "http://api.example.com/v1",
        "https://127.0.0.1/v1",
        "https://192.168.1.2/mcp",
        "https://metadata.google.internal/",
        "https://device.local/",
        "https://[fe80::1]/",
        "https://[::ffff:7f00:1]/",
        "https://168.63.129.16/",
    ] {
        assert!(validate_public_endpoint(url).is_err(), "{url}");
    }
    for url in [
        "https://api.anthropic.com/v1/messages",
        "https://api.openai.com:443/v1/chat/completions",
        "https://mcp.example.com/mcp",
        "https://[2606:4700:4700::1111]/",
    ] {
        let endpoint = validate_public_endpoint(url).unwrap();
        let ip = literal_ip(endpoint.url()).unwrap_or("8.8.8.8".parse().unwrap());
        endpoint.validate_addresses(&[ip]).unwrap();
        assert!(endpoint.validate_addresses(&[]).is_err());
        assert!(endpoint
            .validate_addresses(&[ip, "127.0.0.1".parse().unwrap()])
            .is_err());
    }
    validate_api_key_credential("llm.key.anthropic").unwrap();
    for reference in [
        "connector.oauth.google",
        "connector.oauth.openai-codex",
        "connector.oauth.google.refresh",
    ] {
        assert_eq!(
            validate_api_key_credential(reference).unwrap_err().type_,
            "UnsupportedCapability"
        );
    }
}

#[test]
fn ip_special_ranges_and_ipv4_mapped_addresses() {
    for bad in [
        "0.0.0.0",
        "0.1.2.3",
        "10.0.0.1",
        "100.64.0.1",
        "127.255.255.255",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.0.8",
        "192.0.2.1",
        "192.168.0.1",
        "192.88.99.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::192.168.1.1",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
        "64:ff9b::7f00:1",
        "2001::1",
        "2001:db8::1",
        "2001:20::1",
        "2002:7f00:1::",
        "3fff::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ] {
        assert!(!public_ip(bad.parse().unwrap()), "{bad}");
    }
    for good in [
        "8.8.8.8",
        "1.1.1.1",
        "172.15.255.255",
        "172.32.0.1",
        "192.0.1.1",
        "::ffff:8.8.8.8",
        "2001:4860:4860::8888",
        "2606:4700:4700::1111",
        "2a00:1450:4001::1",
    ] {
        assert!(public_ip(good.parse().unwrap()), "{good}");
    }
}

#[test]
fn dns_rebinding_cannot_change_the_pinned_connection_and_next_hop_resolves_again() {
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let p = policy().with_resolver(move |_: &str| {
        Ok(vec![if counter.fetch_add(1, Ordering::SeqCst) == 0 {
            "8.8.8.8:443".parse().unwrap()
        } else {
            "127.0.0.1:443".parse().unwrap()
        }])
    });
    let pinned = p
        .resolve("POST", "https://api.example.com/v1/messages")
        .unwrap();
    assert_eq!(
        pinned.resolve("api.example.com:443").unwrap(),
        vec!["8.8.8.8:443".parse::<SocketAddr>().unwrap()]
    );
    assert_eq!(
        pinned.resolve("api.example.com:443").unwrap()[0].ip(),
        "8.8.8.8".parse::<IpAddr>().unwrap()
    );
    assert!(pinned.resolve("other.example.com:443").is_err());
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(p
        .resolve("POST", "https://api.example.com/v1/messages")
        .is_err());
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[test]
fn dns_mixed_answers_and_wrong_ports_fail_closed() {
    for addresses in [
        vec!["8.8.8.8:443", "127.0.0.1:443"],
        vec!["[::ffff:7f00:1]:443"],
        vec!["8.8.8.8:80"],
        vec![],
    ] {
        let addresses: Vec<SocketAddr> = addresses.iter().map(|ip| ip.parse().unwrap()).collect();
        let p = policy().with_resolver(move |_: &str| Ok(addresses.clone()));
        assert!(p.resolve("GET", "https://api.example.com/").is_err());
    }
}

#[test]
fn redirects_cannot_hide_normalization_in_join() {
    let p = policy();
    for location in [
        "../account",
        "/v1/spaces/s/../s/query",
        "/v1/%61ccount",
        "http://127.1:7011/v1/account",
        "//127.1:7011/v1/account",
        "http:////127.0.0.1:7011/v1/account",
        "/v1/account#ignored",
        "/v1/account\r\n",
    ] {
        assert!(
            p.redirect("http://127.0.0.1:7011/v1/account", location)
                .is_err(),
            "{location}"
        );
    }
    assert_eq!(
        p.redirect("http://127.0.0.1:7011/v1/account", "/v1/devices")
            .unwrap(),
        "http://127.0.0.1:7011/v1/devices"
    );
}

#[test]
fn data_route_fixture_has_no_duplicate_or_non_explicit_rows() {
    let mut seen = std::collections::BTreeSet::new();
    for row in DATA_ROUTES.lines().filter(|line| !line.starts_with('#')) {
        assert!(seen.insert(row), "duplicate {row}");
        let (method, path) = row.split_once(' ').unwrap();
        assert!(matches!(
            method,
            "GET" | "POST" | "PATCH" | "PUT" | "DELETE"
        ));
        assert!(path.starts_with("/v1/") && !path.contains('*'));
    }
    assert_eq!(seen.len(), 133);
}
