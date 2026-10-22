# 运维部署

## 本地开发

先进入 Nix 开发环境（提供 uv、python、ruff）：

```fish
nix develop

# 按 uv.lock 安装依赖（不升级）
uv sync --locked

# 启动 API
uv run uvicorn panbridge.main:app --host 127.0.0.1 --port 8000 --reload

# 启动 Worker（另开终端）
uv run python -m panbridge.worker
```

> NixOS 下请勿使用 `curl | sh` 安装的 uv（动态链接，无法运行），
> 统一通过 `nix develop` 使用 nixpkgs 提供的 uv。

## 生产部署方向

主要部署方向为 **systemd 原生服务**（后续 M3 阶段完善 NixOS 模块）。

### Nix 打包部署（推荐）

项目已通过 **uv2nix** 全 Nix 化，可离线可复现地构建出 Nix 包：

```fish
# 构建 Nix 包（含 panbridge CLI + panbridge-serve 入口）
nix build

# 直接运行 CLI
nix run . -- auth status

# 直接启动 API 服务
nix run .#serve -- --host 0.0.0.0 --port 8000

# 产物入口
./result/bin/panbridge        # CLI（扫码登录/状态查询等）
./result/bin/panbridge-serve  # API 服务（uvicorn panbridge.main:app）
```

在 NixOS 服务器上引用本仓库 flake 即可安装：

```nix
{
  inputs.panbridge.url = "path:/path/to/panbridge";
  # 或 git 仓库 URL
  # inputs.panbridge.url = "github:your/panbridge";

  outputs = { panbridge, nixpkgs, ... }: {
    nixosConfigurations.server = nixpkgs.lib.nixosSystem {
      modules = [
        {
          environment.systemPackages = [
            panbridge.packages.x86_64-linux.default
          ];
        }
      ];
    };
  };
}
```

> 后续可在 `flake.nix` 里补一个 `nixosModules.panbridge`，用 `panbridge-serve`
> + `python -m panbridge.worker` 组成两个 systemd 服务，实现声明式常驻部署。

### 环境变量

完整配置见 `.env.example`。

生产环境必须设置：

- `PANBRIDGE_API_KEY`：API 鉴权密钥
- `PANBRIDGE_SECRET_KEY`：凭据加密主密钥（多机共享时显式注入；本地单用户可省略，会自动生成到 `~/.config/panbridge/secret.key`）

可选调优：

- `PANBRIDGE_MAX_TASK_CONCURRENCY`：并发任务数（默认 4）
- `PANBRIDGE_MAX_RETRY_ATTEMPTS`：失败重试次数（默认 3）
- `PANBRIDGE_DOWNLOAD_RATE_LIMIT`：下载限速，字节/秒（默认 0 不限速）

## 数据与安全

- 数据库：SQLite WAL（默认），可切换 PostgreSQL（`PANBRIDGE_DATABASE_URL`）
- 凭据加密存储，主密钥本地自动生成（权限 0600）或外部注入
- 扫码登录会话仅存内存，不落盘；明文 Cookie 只在内存中短暂存在
- 默认监听 `127.0.0.1`，不自动开放公网端口
- 遵守网盘服务条款：不实现验证码/风控/付费权限绕过，不做无限重试

## 监控

- 结构化日志（Python logging，后续接入结构化 JSON 日志）
- 任务状态可通过 `/v1/tasks/{id}` 轮询
- SSE / Webhook 推送在后续阶段实现
