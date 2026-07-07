{
  description = "anybao — agent harness + isolated runtime on top of any";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python313
            basedpyright   # python LSP (emacs eglot/lsp-mode); resolves the
                           # uv venv via pyrightconfig.json
            ruff           # editor-facing ruff/ruff-lsp (CI uses the uv one)
          ];

          # uv manages the workspace venv; keep it from downloading its own
          # interpreter so the environment stays nix-pinned.
          env = {
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON = "python3.13";
          };

          shellHook = ''
            echo "anybao dev shell — uv $(uv --version 2>/dev/null | cut -d' ' -f2), $(python3 --version)"
          '';
        };
      });
}
