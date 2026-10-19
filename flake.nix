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
        # 统一 Python 版本：所有开发工具（uv、ruff、pytest）基于 nixpkgs 提供。
        python = pkgs.python312;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python
            uv
            ruff
            sqlite
            stdenv.cc.cc.lib
          ];

          # 强制 uv 使用 Nix 提供的 Python，禁止 uv 自行下载解释器
          # （NixOS 无法运行动态链接的官方 Python 构建，会导致
          #  "Could not start dynamically linked executable" 错误）。
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = "${python}/bin/python";
          # 项目级虚拟环境放在本地 .venv。
          UV_PROJECT_ENVIRONMENT = ".venv";

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"

            # 确保 Nix 提供的 uv/python 优先于 ~/.local/bin 等外部二进制。
            # uv 官方安装脚本会在 ~/.local/bin/env.fish 里前置该目录，
            # 其中的 python 是动态链接构建，NixOS 无法运行，必须被 Nix 覆盖。
            export PATH="${pkgs.uv}/bin:${python}/bin:${pkgs.ruff}/bin:$PATH"

            # 强制 uv 使用 Nix 提供的 Python，禁止 uv 自行下载解释器
            # （NixOS 无法运行动态链接的官方 Python 构建）。
            export UV_PYTHON_DOWNLOADS=never
            export UV_PYTHON="${python}/bin/python"

            echo "PanBridge 开发环境已就绪 (Nix + uv)"
            echo "  uv sync --locked    # 按 uv.lock 安装依赖（不升级）"
            echo "  uv run pytest       # 运行测试"
            echo "  uv run panbridge auth login   # 扫码登录"
          '';
        };
      });
}
