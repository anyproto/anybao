.PHONY: kernel runtime runtime-check test test-integration lint

# Componentized CPython guest (runtime/guest + runtime/wit -> bin/kernel.wasm).
# ~1.4s build; the guest tests need it. Artifact is gitignored.
kernel: bin/kernel.wasm

bin/kernel.wasm: runtime/guest/app.py runtime/wit/kernel.wit
	mkdir -p bin
	uv run componentize-py -d runtime/wit -w kernel componentize -p runtime/guest app -o bin/kernel.wasm

# The Rust runtime (runtime/target/release/anyrt).
runtime:
	cargo build --release --manifest-path runtime/Cargo.toml

runtime-check:     ## clippy + fmt gate for runtime/
	cargo clippy --manifest-path runtime/Cargo.toml -- -D warnings
	cargo fmt --manifest-path runtime/Cargo.toml --check

# Full offline suite: build the kernel, cargo unit tests, then the
# guest-module + wire pytest (rt_e2e needs the release binary — build it
# with `make runtime` first).
test: kernel
	cargo test --manifest-path runtime/Cargo.toml
	uv run pytest

# Integration tests against a real any server (skipped without one).
# Start a server first: `any run --addr 127.0.0.1:7009` (or set
# ANYBAO_TEST_SERVER). Validates the wire contract offline fakes can't.
test-integration: kernel
	uv run pytest -m integration

lint: runtime-check
	uv run ruff check .
