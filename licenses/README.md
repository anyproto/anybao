# Maintaining license notices

Release archives ship the project license, third-party index, this
directory, and `runtime/guest/licenses/`. The Rust crate carries its
own copy of the project license. The two deployed overlay READMEs
include the complete MIT text because `deploy` publishes READMEs but
does not publish arbitrary files such as LICENSE.

Run the offline check before packaging:

```sh
make licenses-check
```

It checks notice hashes, reviewed dependency inputs, the pinned
componentize-py version, and the project-license copies. CI and the
release workflow run the same check. `tools/check_licenses.py --archive
<tar.gz>` also verifies that every recorded notice is present and
unchanged in a release archive.

## Rust dependency updates

Install the pinned generator, then regenerate the runtime notices:

```sh
cargo install cargo-about --version 0.9.2 --locked
cargo-about generate --locked --all-features --fail \
  --manifest-path runtime/Cargo.toml --config licenses/about.toml \
  --output-file licenses/RUST_DEPENDENCIES.md licenses/about.hbs
```

Review the generated diff and license selections. Compound `AND`
expressions retain every required term; `OR` expressions select an
accepted alternative. `about.toml` preserves libm's complete composite
license and attribution text with a checksum. Do not replace actual
copyright notices with generic SPDX templates.

## Kernel or vendored Python updates

Review [KERNEL_COMPONENTS.md](KERNEL_COMPONENTS.md) against the new
componentize-py wheel, build script, SDK references, and upstream
licenses. Use `about-kernel.toml` and `about-kernel.hbs` to regenerate
`KERNEL_RUST_DEPENDENCIES.md` from the upstream `runtime/Cargo.toml`
with its unchanged Cargo.lock. Check for missing manifest licenses and
for new native or standard-library components; a Cargo inventory alone
does not cover the Python/WASI payload.

For vendored packages, copy the original upstream license files as well
as the code. Update `runtime/guest/VENDORED.md` and
`vendored-python.json`, retaining exact versions, wheel URLs and hashes.
Keep `kernel/sources.json` and `kernel/build.json` aligned with the
reviewed kernel sources. Notice records describe provenance, not a
license grant of our own.

After reviewing the notices and dependency inputs, record their hashes:

```sh
uv run --locked python tools/check_licenses.py --record
make licenses-check
```

Commit the notices, provenance records, and manifest together. Recording
hashes does not establish license correctness; it prevents the reviewed
files from silently falling out of sync. Changes to fixture content or
contributor ownership require their own provenance review.
