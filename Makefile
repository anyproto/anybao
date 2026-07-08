.PHONY: kernel test lint api-drift api-vendor

# Drift check: vendored any swagger vs the coverage manifest (plan §4).
# Nonzero exit on drift so CI catches an uncovered/changed endpoint.
api-drift:
	uv run python -m anybao.apidrift

# Re-vendor any's swagger (run after bumping the targeted any version),
# then re-check drift to see what moved.
api-vendor:
	cp ../any/internal/server/docs/swagger.json api/swagger.vendored.json
	uv run python -m anybao.apidrift || true


# Componentized CPython guest (runtime/guest + runtime/wit -> bin/kernel.wasm).
# ~1.4s build; CI runs this before pytest. Artifact is gitignored.
kernel: bin/kernel.wasm

bin/kernel.wasm: runtime/guest/app.py runtime/wit/kernel.wit
	mkdir -p bin
	uv run componentize-py -d runtime/wit -w kernel componentize -p runtime/guest app -o bin/kernel.wasm

test: kernel
	uv run pytest

# Integration tests against a real any server (skipped without one).
# Start a server first: `any run --addr 127.0.0.1:7009` (or set
# ANYBAO_TEST_SERVER). Validates the wire contract offline fakes can't.
test-integration: kernel
	uv run pytest -m integration

lint:
	uv run ruff check .
