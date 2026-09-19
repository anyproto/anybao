# Third-party notices

Anybao and anyrt's original code is licensed under [MIT](LICENSE),
copyright 2026 Any Association. Third-party components retain their
own licenses; the project's MIT license does not replace them.

| Component | License information |
|---|---|
| Rust runtime dependencies, including the optional shell feature | [Locked dependency notices](licenses/RUST_DEPENDENCIES.md) |
| Embedded Python runtime and its Rust dependencies | [Kernel sources and notices](licenses/KERNEL_COMPONENTS.md) |
| Beautiful Soup, Soup Sieve, markdownify, six, typing_extensions | [Versions](runtime/guest/VENDORED.md) and [original notices](runtime/guest/licenses/) |

Binary release archives include this file, `LICENSE`, `licenses/`, and
`runtime/guest/licenses/`. Preserve the applicable notices when
redistributing the executable or vendored source. Agent and connector
overlays carry the MIT text in their published READMEs.

The inventories include dependencies for multiple targets and some
build-time components, so a listed component is not necessarily linked
into every binary. Development tools and connected services retain
their own terms. Their inclusion in a build environment or connector
does not relicense them under MIT.

See [licenses/README.md](licenses/README.md) for updating and checking
these files when dependencies change.
