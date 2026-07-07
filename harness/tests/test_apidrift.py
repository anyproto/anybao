from anybao import apidrift

SPEC = {
    "paths": {
        "/v1/spaces": {
            "get": {"parameters": [{"name": "status", "in": "query"}],
                    "responses": {"200": {}}},
            "post": {"parameters": [{"name": "body", "in": "body",
                                     "schema": {"$ref": "#/definitions/SpaceCreate"}}],
                     "responses": {"201": {}}},
        },
        "/v1/spaces/{id}": {"delete": {"responses": {"204": {}}}},
    }
}


def test_extract_endpoints():
    eps = apidrift.extract_endpoints(SPEC)
    assert set(eps) == {"GET /v1/spaces", "POST /v1/spaces", "DELETE /v1/spaces/{id}"}
    assert all(fp.startswith("sha256:") for fp in eps.values())


def test_clean_against_itself():
    eps = apidrift.extract_endpoints(SPEC)
    manifest = apidrift.build_skeleton(eps)
    assert apidrift.diff(eps, manifest).clean()


def test_detects_new_removed_changed():
    eps = apidrift.extract_endpoints(SPEC)
    manifest = apidrift.build_skeleton(eps)

    # removed: manifest has an endpoint the spec dropped
    manifest["endpoints"]["GET /v1/gone"] = {"fingerprint": "sha256:x"}
    # changed: mutate a fingerprint the manifest pinned
    manifest["endpoints"]["GET /v1/spaces"]["fingerprint"] = "sha256:stale"
    # new: add an endpoint to the spec not in the manifest
    spec2 = {**SPEC, "paths": {**SPEC["paths"],
             "/v1/new": {"get": {"responses": {"200": {}}}}}}
    eps2 = apidrift.extract_endpoints(spec2)

    d = apidrift.diff(eps2, manifest)
    assert "GET /v1/new" in d.new
    assert "GET /v1/gone" in d.removed
    assert "GET /v1/spaces" in d.changed
    assert not d.clean()


def test_fingerprint_ignores_prose():
    a = {"parameters": [{"name": "x", "in": "query"}], "responses": {"200": {}},
         "summary": "old", "description": "old", "tags": ["a"]}
    b = {"parameters": [{"name": "x", "in": "query"}], "responses": {"200": {}},
         "summary": "NEW", "description": "totally different", "tags": ["b"]}
    assert apidrift.endpoint_fingerprint(a) == apidrift.endpoint_fingerprint(b)


def test_fingerprint_catches_new_required_param():
    a = {"parameters": [{"name": "x", "in": "query"}], "responses": {"200": {}}}
    b = {"parameters": [{"name": "x", "in": "query"},
                        {"name": "y", "in": "query", "required": True}],
         "responses": {"200": {}}}
    assert apidrift.endpoint_fingerprint(a) != apidrift.endpoint_fingerprint(b)
