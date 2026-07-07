.PHONY: kernel test lint

# Componentized CPython guest (runtime/guest + runtime/wit -> bin/kernel.wasm).
# ~1.4s build; CI runs this before pytest. Artifact is gitignored.
kernel: bin/kernel.wasm

bin/kernel.wasm: runtime/guest/app.py runtime/wit/kernel.wit
	mkdir -p bin
	uv run componentize-py -d runtime/wit -w kernel componentize -p runtime/guest app -o bin/kernel.wasm

test: kernel
	uv run pytest

lint:
	uv run ruff check .
