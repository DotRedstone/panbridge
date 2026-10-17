{
  description = "PanBridge 多网盘自动转存与下载后端开发环境";

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
          buildInputs = with pkgs; [
            python312
            uv
            ruff
            sqlite
          ];

          shellHook = ''
            echo "PanBridge 开发环境已就绪"
            echo "  uv sync          # 安装依赖"
            echo "  uv run pytest    # 运行测试"
          '';
        };
      });
}
