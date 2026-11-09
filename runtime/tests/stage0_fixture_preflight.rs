//! Opt-in real local server integration; no provider request or wasm timing.
#![cfg(feature = "android-spike")]

#[test]
#[ignore = "requires STAGE0_FIXTURE_PLAN for an explicitly provisioned scratch server"]
fn pinned_scratch_plan_constructs_real_broker_without_provider_access() {
    let path = std::env::var("STAGE0_FIXTURE_PLAN").expect("explicit scratch plan required");
    let plan: serde_json::Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    let mut request = plan["turns"][0].clone();
    request["base_url"] = plan["server"]["base_url"].clone();
    request["fixture_dir"] = plan["desktop_fixture_dir"].clone();
    request["provider_key"] = serde_json::json!("local-preflight-sentinel-never-sent");
    request["config"] = serde_json::json!({"llm.tier.codegen": {
        "provider":"openai-compat", "backend":"openai", "model":"explicit-preflight-model",
        "profile":"gpt", "base_url":"https://api.openai.com/v1", "api_key_ref":"llm.key.openai"
    }});
    let prepared =
        anyrt::stage0_fixture::prepare_turn(serde_json::from_value(request).unwrap()).unwrap();
    assert_eq!(prepared.spec, "android-fixture@v1");
    assert!(prepared
        .broker
        .writer
        .records
        .iter()
        .any(|r| r["effect"] == "http.get"
            && r["input"]["url"]
                .as_str()
                .is_some_and(|url| url.ends_with("/v1/health"))
            && r["error"].is_null()));
    assert!(!prepared
        .broker
        .writer
        .records
        .iter()
        .any(|r| r["effect"] == "http.post"));
}
