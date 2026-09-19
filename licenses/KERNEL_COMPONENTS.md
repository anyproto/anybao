# Embedded Python kernel

The kernel is produced by **componentize-py 0.24.0**. Python 3.13 in the
build instructions is the host interpreter; this componentize-py
release embeds a **CPython 3.14** fork.

| Component | Pinned source | Notices |
|---|---|---|
| CPython and incorporated standard-library code | `dicej/cpython`, `v3.14.0-wasi-sdk-30` | [Python license](kernel/CPython-LICENSE.txt), [incorporated software](kernel/CPython-incorporated-software.rst) |
| Expat | bundled with the pinned CPython source | [COPYING](kernel/Expat-COPYING.txt) |
| HACL* | bundled with the pinned CPython source | [License header](kernel/HACL-LICENSE.txt) |
| libmpdec | bundled with the pinned CPython source | libmpdec section of the [incorporated-software notices](kernel/CPython-incorporated-software.rst) |
| zlib | 1.3.1 | [License](kernel/zlib-LICENSE.txt) |
| SQLite | 3.51.2 | [Public-domain dedication](https://www.sqlite.org/copyright.html) |
| WASI libc | `161b3195fc2558d2b1ba3eb9ffae3b2b47407623`, from WASI SDK 33 | [License overview](kernel/wasi-libc-LICENSE.txt), accompanying `wasi-libc-*` files |
| LLVM libraries (libc++, libc++abi, compiler runtime, libunwind) | `4434dabb69916856b824f68a64b029c67175e532`, from WASI SDK 33 | accompanying `*-LICENSE.txt` files in [kernel/](kernel/) |
| componentize-py-runtime and Rust dependencies | componentize-py `v0.24.0`, locked dependency graph | [Rust license texts](KERNEL_RUST_DEPENDENCIES.md) |

Sources are established by the pinned componentize-py
[build script](https://github.com/bytecodealliance/componentize-py/blob/v0.24.0/build.rs)
and [wheel release workflow](https://github.com/bytecodealliance/componentize-py/blob/v0.24.0/.github/workflows/release.yaml).
[kernel/sources.json](kernel/sources.json) records the exact notice URLs,
hashes, and any extraction. Most files are copied verbatim; the HACL*
and emmalloc files retain their complete leading source comments.

The `wit-dylib-ffi` manifest at `dicej/wasm-tools` commit
`b072b0caa8307779558d96a62bc9522abda6a7fb` omits a license field. Its
[repository license statement](https://github.com/dicej/wasm-tools/blob/b072b0caa8307779558d96a62bc9522abda6a7fb/README.md#license)
licenses the project under Apache-2.0, Apache-2.0 with LLVM exception,
or MIT. `about-kernel.toml` records the MIT selection with the exact
upstream license-file checksum.

This bundle covers the pinned upstream wheel build and the checked-in
guest libraries. If you build componentize-py from modified sources,
replace the SDK, or add kernel libraries, review and update their
notices as well. The inventories are not a certification of ownership
or of every possible custom build.
