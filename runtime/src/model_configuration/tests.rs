use super::*;
fn fixtures() -> Value {
    serde_json::from_str(include_str!(
        "../../../docs/fixtures/chatgpt-model-config-v1.json"
    ))
    .unwrap()
}

#[test]
fn shared_decoded_json_identity_fixtures() {
    for case in fixtures()["canonical"].as_array().unwrap() {
        let value = serde_json::from_str::<Value>(case["json"].as_str().unwrap()).ok();
        let encoded = value.as_ref().and_then(canonical);
        let id = value.as_ref().and_then(config_id);
        assert_eq!(json!(encoded), case["canonical"], "{}", case["name"]);
        assert_eq!(json!(id), case["configId"], "{}", case["name"]);
    }
}

#[test]
fn shared_raw_snapshot_fixtures() {
    for case in fixtures()["snapshots"].as_array().unwrap() {
        let rows = case["rows"].as_array().unwrap();
        let overrides = serde_json::from_value(case["overrides"].clone()).unwrap();
        assert_eq!(
            verdict(rows, &overrides).configuration,
            case["runtimeKind"],
            "{}",
            case["name"]
        );
    }
}

#[test]
fn invalid_encoding_and_target_never_match() {
    assert!(!same(&json!(1e30), &json!(1e30)));
    assert!(parse_target(&json!("null")).is_none());
    let mut nested = json!(null);
    for _ in 0..34 {
        nested = json!([nested]);
    }
    assert!(canonical(&nested).is_none());
    assert!(canonical(&json!("x".repeat(70_000))).is_none());
}

#[test]
fn shared_raw_target_fixtures() {
    for case in fixtures()["targets"].as_array().unwrap() {
        assert_eq!(
            parse_target(&case["json"]).is_some(),
            case["valid"].as_bool().unwrap(),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn search_compatibility_matches_each_guest_provider_boundary() {
    for case in fixtures()["searchCompatibility"].as_array().unwrap() {
        let name = case["key"].as_str().unwrap();
        let accepted = case["compatible"].as_bool().unwrap();
        assert_eq!(
            compatible_search(name, &case["row"]),
            accepted,
            "{}",
            case["name"]
        );
        let mut rows = fixtures()["snapshots"][0]["rows"]
            .as_array()
            .unwrap()
            .clone();
        let manifest = rows.iter_mut().find(|r| r["id"] == TARGET_KEY).unwrap();
        let mut target = parse_target(&manifest["value"]).unwrap();
        target.search.insert(name.into(), case["row"].clone());
        manifest["value"] = json!(serde_json::to_string(&target).unwrap());
        rows.push(json!({"id":format!("search.provider.{name}"),
            "key":format!("search.provider.{name}"), "value":case["row"]}));
        assert_eq!(
            verdict(&rows, &BTreeMap::new()).configuration,
            if accepted { "matched" } else { "unsupported" },
            "{}",
            case["name"]
        );
    }
}

#[test]
fn current_anyai_and_explicit_search_transition_are_preserved() {
    let local = json!({"provider":"openai-compat","backend":"anyai","model":"codex/default","base_url":"http://127.0.0.1:23456/v1","api_key_ref":"llm.key.anyai","traits":{"web_search":true}});
    assert!(compatible(&local));
    let tiers: BTreeMap<_, _> = TIERS
        .into_iter()
        .map(|t| (t.to_owned(), local.clone()))
        .collect();
    let search = BTreeMap::from([(
        "websearch".into(),
        json!({"provider":"gemini","model":"synthetic","base_url":"https://generativelanguage.googleapis.com","api_key_ref":"google.key.gemini"}),
    )]);
    let target = Target {
        schema_version: 1,
        apply_id: "synthetic".into(),
        tiers: tiers.clone(),
        search: search.clone(),
    };
    let mut rows: Vec<_> = TIERS
        .into_iter()
        .map(|t| json!({"id":format!("llm.tier.{t}"),"key":format!("llm.tier.{t}"),"value":local}))
        .collect();
    rows.push(
        json!({"id":TARGET_KEY,"key":TARGET_KEY,"value":serde_json::to_string(&target).unwrap()}),
    );
    rows.push(
        json!({"id":"llm.tier.judge","key":"llm.tier.judge","value":{"provider":"typesafe"}}),
    );
    assert_eq!(verdict(&rows, &BTreeMap::new()).configuration, "incomplete");
    rows.push(json!({"id":"search.provider.websearch","key":"search.provider.websearch","value":search["websearch"]}));
    let complete = verdict(&rows, &BTreeMap::new());
    assert_eq!(complete.configuration, "matched");
    assert_eq!(
        complete.config_id,
        config_id(&target_identity(&tiers, &search))
    );
    rows.last_mut().unwrap()["value"] = local;
    assert_eq!(verdict(&rows, &BTreeMap::new()).configuration, "incomplete");
}
