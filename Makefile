.PHONY: api-drift kernel runtime runtime-shell runtime-check test test-runtime test-integration lint licenses-check

# Componentized CPython guest (runtime/guest + runtime/wit -> bin/kernel.wasm).
# ~1.4s build; the guest tests need it. Artifact is gitignored.
kernel: bin/kernel.wasm

bin/kernel.wasm: runtime/guest/app.py runtime/wit/kernel.wit
	mkdir -p bin
	uv run componentize-py -d runtime/wit -w kernel componentize -p runtime/guest app -o bin/kernel.wasm

# The Rust runtime (runtime/target/release/anyrt). The kernel wasm is
# EMBEDDED into the binary (include_bytes!, ADR-009 §4), so the cargo
# build needs bin/kernel.wasm to exist first.
runtime: kernel
	cargo build --release --manifest-path runtime/Cargo.toml

# Same binary WITH shell effects (`sh.*`/`fs.*` syscalls + the `bash`
# tool, ADR-024 §6). Off by default so any-ui's path dependency never
# ships a shell; this is the build a coding bao runs.
runtime-shell: kernel
	cargo build --release --features shell --manifest-path runtime/Cargo.toml

api-drift: runtime      ## vendored openapi vs coverage manifest (nonzero on drift)
	./runtime/target/release/anyrt drift

runtime-check: kernel     ## clippy + fmt gate for runtime/ (both feature sets)
	cargo clippy --manifest-path runtime/Cargo.toml -- -D warnings
	cargo clippy --features shell --manifest-path runtime/Cargo.toml -- -D warnings
	cargo fmt --manifest-path runtime/Cargo.toml --check

# Runtime unit tests for one feature set: `make test-runtime` (default
# features) or `make test-runtime FEATURES=shell` (the shell effects +
# their tests, ADR-024). CI runs both legs; `test` below runs the shell
# one, the superset.
test-runtime: kernel
	cargo test $(if $(FEATURES),--features $(FEATURES)) --manifest-path runtime/Cargo.toml

# Full offline suite: build the kernel, cargo unit tests, then the
# guest-module + wire pytest (rt_e2e needs the release binary — build it
# with `make runtime` first).
test: kernel
	$(MAKE) test-runtime FEATURES=shell
	uv run pytest

# Integration tests against a real any server (skipped without one).
# Start a server first: `any run --addr 127.0.0.1:7009` (or set
# ANYBAO_TEST_SERVER). Validates the wire contract offline fakes can't.
test-integration: kernel
	uv run pytest -m integration

lint: runtime-check
	uv run ruff check .

licenses-check:
	uv run --locked python tools/check_licenses.py
