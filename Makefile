.PHONY: api-drift kernel runtime runtime-shell runtime-cross kernel-cwasm runtime-precompiled runtime-pulley runtime-android kernel-check-precompiled runtime-check test test-runtime test-integration lint licenses-check

# Componentized CPython guest (runtime/guest + runtime/wit -> bin/kernel.wasm).
# ~1.4s build; the guest tests need it. Artifact is gitignored.
kernel: bin/kernel.wasm

# compileall first: from a tree without .pyc files the pre-init snapshot
# keeps the compiler's heap (+2 MB today, +25 MB with pypdf; ADR-031 §7)
bin/kernel.wasm: runtime/guest/app.py runtime/wit/kernel.wit
	mkdir -p bin
	uv run python -m compileall -q runtime/guest
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

# ---- precompiled kernel (ADR-032 §2) -------------------------------------
# A `cross-compile` host anyrt compiles bin/kernel.wasm for a target into
# bin/kernel.cwasm (+ .sha256); a `precompiled-kernel` build embeds that
# instead of the wasm. The .cwasm loads only into an engine configured
# exactly like the one that compiled it, so it is rebuilt with the
# binary, every time, and never shipped on its own.
HOST_TRIPLE := $(shell rustc -vV 2>/dev/null | sed -n 's/^host: //p')
CWASM_TARGET ?= $(HOST_TRIPLE)
CWASM_PLATFORM ?=

runtime-cross: kernel
	cargo build --release --features cross-compile --target-dir runtime/target/cross --manifest-path runtime/Cargo.toml

kernel-cwasm: runtime-cross
	runtime/target/cross/release/anyrt kernel compile --target $(CWASM_TARGET) $(if $(CWASM_PLATFORM),--platform $(CWASM_PLATFORM)) -o bin/kernel.cwasm

# this host, cranelift-aot: runtime/target/precompiled/release/anyrt
runtime-precompiled: kernel-cwasm
	cargo build --release --features precompiled-kernel --target-dir runtime/target/precompiled --manifest-path runtime/Cargo.toml

# this host, pulley (the iOS fallback, ADR-032 §11), desktop knobs:
# runtime/target/pulley/release/anyrt
runtime-pulley:
	$(MAKE) kernel-cwasm CWASM_TARGET=pulley64 CWASM_PLATFORM=$(or $(CWASM_PLATFORM),desktop)
	cargo build --release --features precompiled-kernel,pulley --target-dir runtime/target/pulley --manifest-path runtime/Cargo.toml

# the on-device smoke test, here: both precompiled builds must pass and
# spend the same fuel as the JIT build (ADR-032 §1)
kernel-check-precompiled: runtime runtime-precompiled
	runtime/target/release/anyrt kernel check
	runtime/target/precompiled/release/anyrt kernel check

# The phone build: anyrt for aarch64-linux-android with the kernel
# precompiled here, so the phone never runs Cranelift. The app ships the
# result as jniLibs/arm64-v8a/libanyrt.so. Needs cargo-ndk and
# ANDROID_NDK_HOME.
ANDROID_API ?= 28
runtime-android:
	$(MAKE) kernel-cwasm CWASM_TARGET=aarch64-linux-android
	cd runtime && cargo ndk -t arm64-v8a -P $(ANDROID_API) build --release --features precompiled-kernel --target-dir target/android
	@echo "-> runtime/target/android/aarch64-linux-android/release/anyrt"

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

# Integration tests against a real any server. They create a space per
# test, so ANYBAO_TEST_SERVER is required and must name a SCRATCH server
# (`any run --data-dir <scratch> --addr 127.0.0.1:<port>`); without it the
# run aborts. Validates the wire contract offline fakes can't.
test-integration: kernel
	uv run pytest -m integration

lint: runtime-check
	uv run ruff check .

licenses-check:
	uv run --locked python tools/check_licenses.py
