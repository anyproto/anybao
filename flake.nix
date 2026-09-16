{
  description = "anybao — agent harness + isolated runtime on top of any";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # the runtime (runtime/): wasmtime's MSRV outruns nixpkgs' rustc, so
    # the toolchain comes from the overlay, pinned like everything else
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ rust-overlay.overlays.default ];
        };
        # channel + components come from ./rust-toolchain.toml — the one
        # pin CI and release install too, so the shell never drifts from
        # what the workflows build with
        rustToolchain = pkgs.rust-bin.fromRustupToolchainFile ./rust-toolchain.toml;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python313
            basedpyright   # python LSP (emacs eglot/lsp-mode); resolves the
                           # uv venv via pyrightconfig.json
            ruff           # editor-facing ruff/ruff-lsp (CI uses the uv one)

            rustToolchain  # cargo/rustc/clippy/rustfmt for runtime/
                           # (rustls-only deps — no openssl/pkg-config)
          ];

          # uv manages the workspace venv; keep it from downloading its own
          # interpreter so the environment stays nix-pinned.
          env = {
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON = "python3.13";
          };

          shellHook = ''
            echo "anybao dev shell — uv $(uv --version 2>/dev/null | cut -d' ' -f2), $(python3 --version), $(rustc --version 2>/dev/null)"
          '';
        };
      });
}
