{
  description = "PanBridge 多网盘自动转存与下载后端（全 Nix 化打包）";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    # uv2nix 生态：把 uv.lock 里的 PyPI 依赖转成 Nix derivation。
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";

    # 保证这些工具共用同一份 nixpkgs，避免版本漂移。
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
  };

  outputs =
    { self
    , nixpkgs
    , flake-utils
    , uv2nix
    , pyproject-nix
    , pyproject-build-systems
    , ...
    }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;

        # 1. 解析 pyproject.toml + uv.lock，得到工作区元数据。
        workspace = uv2nix.lib.workspace.loadWorkspace {
          workspaceRoot = ./.;
        };

        # 2. 由 uv.lock 生成依赖 overlay；wheel 优先，减少原生扩展源码编译。
        uvLockedOverlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };

        # 3. 针对构建系统声明不完整 / 需要特殊处理的包做 override。
        #    quarkpan / aligo / p115client / cloud189 等第三方网盘 SDK
        #    大多只声明了 setuptools（或根本没声明），需要显式补全。
        #    resolveBuildSystem 由 build-system-pkgs 的 overlay 注入到 final 作用域。
        pyprojectOverrides = final: prev: {
          # 主项目本身：声明 setuptools + wheel 构建后端。
          panbridge = prev.panbridge.overrideAttrs (old: {
            nativeBuildInputs =
              (old.nativeBuildInputs or [ ])
              ++ final.resolveBuildSystem { setuptools = [ ]; wheel = [ ]; };
          });

          # 第三方网盘 SDK 及其传递依赖中，构建系统声明不完整的包。
          # 它们 sdist 里没有 pyproject.toml，或没在 build-system.requires 里
          # 声明 setuptools，需要在这里显式补全，否则 wheel 构建失败。
          qrcode-terminal = prev.qrcode-terminal.overrideAttrs (old: {
            nativeBuildInputs =
              (old.nativeBuildInputs or [ ])
              ++ final.resolveBuildSystem { setuptools = [ ]; };
          });
        };

        # 4. 组合构建系统 overlay + 依赖 overlay + 自定义 override，
        #    得到最终的 Python 包集合。wheel 优先（与 sourcePreference 一致），
        #    空 build-system 的包会自动回退到 setuptools+wheel。
        pythonSet =
          (pkgs.callPackage pyproject-nix.build.packages { inherit python; })
          .overrideScope (
            nixpkgs.lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              uvLockedOverlay
              pyprojectOverrides
            ]
          );

        # 5. 生成运行时虚拟环境（仅生产依赖，不含 dev 依赖）。
        #    p115client 的传递依赖（ChenyangGao 系列小工具包）都往
        #    site-packages 塞了一份相同的 LICENSE 文件，内容一致，
        #    用 venvIgnoreCollisions 忽略这类碰撞。
        runtimeEnv = (pythonSet.mkVirtualEnv "panbridge-env" workspace.deps.default)
          .overrideAttrs (old: { venvIgnoreCollisions = [ "*" ]; });

        # 项目名（必须与 pyproject.toml 的 [project.name] 一致）。
        projectName = "panbridge";
        pkg = pythonSet.${projectName};
      in
      {
        # 可构建的 Nix 包：包含 panbridge CLI + uvicorn 启动入口。
        packages.default = pkgs.stdenv.mkDerivation {
          pname = pkg.pname;
          version = pkg.version;
          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          installPhase = ''
            runHook preInstall

            # 运行时环境整包安装到 $out。
            # mkVirtualEnv 已为 workspace 成员自动生成 bin/panbridge 入口，
            # 以及 bin/uvicorn 等命令行工具，直接复制即可。
            mkdir -p $out
            cp -r ${runtimeEnv}/. $out/

            # cp -r 会保留 venv 的只读目录权限，需恢复可写才能追加新文件。
            chmod -R u+w $out/bin $out/lib 2>/dev/null || true

            # 追加一个便捷入口：启动 API 服务（uvicorn panbridge.main:app）。
            makeWrapper ${runtimeEnv}/bin/uvicorn $out/bin/panbridge-serve \
              --add-flags "panbridge.main:app"

            runHook postInstall
          '';
        };

        # nix run . 直接运行 CLI。
        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/panbridge";
        };

        # nix run .#serve 直接启动 API 服务。
        apps.serve = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/panbridge-serve";
        };

        # 开发环境：保留 uv 工作流（impure），便于快速迭代。
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python
            uv
            ruff
            sqlite
            stdenv.cc.cc.lib
          ];

          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = "${python}/bin/python";
          UV_PROJECT_ENVIRONMENT = ".venv";

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
            export PATH="${pkgs.uv}/bin:${python}/bin:${pkgs.ruff}/bin:$PATH"
            export UV_PYTHON_DOWNLOADS=never
            export UV_PYTHON="${python}/bin/python"

            echo "PanBridge 开发环境已就绪 (Nix + uv)"
            echo "  uv sync --locked               # 按 uv.lock 安装依赖"
            echo "  uv run panbridge auth login    # 扫码登录"
            echo "  nix build .#default            # 构建 Nix 包（全 Nix 化）"
            echo "  nix run .                      # 直接运行 CLI"
          '';
        };
      });
}
