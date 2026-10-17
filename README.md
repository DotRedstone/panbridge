# PanBridge

多网盘自动转存与下载后端服务。用户通过 HTTP API 提交网盘分享链接，服务自动识别平台、解析分享内容，并使用已配置的网盘账号完成转存与下载。

## 支持平台

| 平台 | 登录方式 | 能力 | 验证状态 |
|------|---------|------|---------|
| 夸克网盘 | 扫码登录 | 解析 / 转存 / 下载 | ⚠️ 需真实账号做端到端验证 |
| 阿里云盘 | refresh_token（手动） | 解析 / 预览（**无需登录**）/ 转存 / 下载 | ✅ 解析已真实验证 |
| Mock | 无需 | 全链路（测试/演示） | ✅ 完整可用 |

> 阿里云盘的「解析分享、预览文件列表」**无需登录**即可工作（已用真实公开
> 分享验证）；只有「转存到自己网盘」需要 refresh_token。
> 其余平台（123/UC/百度/115/迅雷）暂未接入，调研结论见
> [docs/providers.md](docs/providers.md)。

## 特性

- 自动识别网盘链接与提取码（夸克/阿里/123/UC/百度/115/迅雷/天翼等域名）
- 解析分享文件与目录
- 使用已授权账号转存文件
- 下载文件到本地（内置 HTTP 流式下载，支持限速、断点续传）
- 持久化后台任务，支持状态查询、结果、失败原因、取消与重试
- 并发任务执行（可配置 `max_task_concurrency`）
- 统一 REST API（`/v1` 前缀），OpenAPI 文档
- 不同网盘使用独立 Adapter，便于扩展
- 账号凭据本地加密存储，不写死在代码中

## 快速开始

### 使用 Nix 开发环境（NixOS 推荐）

项目通过 [Nix Flake](https://nixos.wiki/wiki/Flakes) 统一管理开发工具链
（Python、uv、ruff、sqlite），所有工具由 nixpkgs 提供，不依赖 curl/pip
官方安装脚本。首次运行会拉取并锁定依赖（生成 `flake.lock`），之后可复用。

```fish
# 进入开发环境（提供 python/uv/ruff/sqlite）
nix develop

# 按 uv.lock 安装 Python 依赖（不升级）
uv sync --locked

# 扫码登录（终端显示二维码，夸克 App 扫描后自动完成）
uv run panbridge auth login
```

也支持一条命令直接运行：

```fish
nix develop -c uv run panbridge auth login
```

#### NixOS + fish 说明

- 开发环境已设置 `UV_PYTHON_DOWNLOADS=never`，强制 uv 使用 Nix 提供的
  Python，避免下载动态链接的官方解释器（NixOS 无法运行，会报
  `Could not start dynamically linked executable`）。
- `shellHook` 会将 Nix 提供的 uv/python/ruff 前置到 `PATH`，覆盖
  `~/.local/bin` 里由 uv 官方安装脚本写入的动态链接二进制。
- 若之前用 `curl | sh` 装过 uv，建议移除 `~/.local/bin/uv`、`~/.local/bin/uvx`
  及 `~/.local/share/uv`，避免与 Nix 提供的 uv 混淆。

#### 非 Nix 环境（可选）

也可直接用 uv 管理依赖：

```bash
uv sync
uv run uvicorn panbridge.main:app --host 127.0.0.1 --port 8000
uv run python -m panbridge.worker
```

### 配置

通过环境变量配置，参见 `.env.example`。关键项：

| 变量 | 说明 | 默认 |
|------|------|------|
| `PANBRIDGE_API_KEY` | API 鉴权密钥（为空则本机免鉴权） | 空 |
| `PANBRIDGE_SECRET_KEY` | 凭据加密主密钥（**可选**，未设置时首次运行自动生成） | 自动生成 |
| `PANBRIDGE_DATABASE_URL` | 数据库连接 | `sqlite+aiosqlite:///./panbridge.db` |
| `PANBRIDGE_DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `PANBRIDGE_BIND_HOST` / `PANBRIDGE_BIND_PORT` | 监听地址 | `127.0.0.1` / `8000` |
| `PANBRIDGE_MAX_TASK_CONCURRENCY` | 并发任务数 | `4` |
| `PANBRIDGE_MAX_RETRY_ATTEMPTS` | 失败重试次数 | `3` |
| `PANBRIDGE_DOWNLOAD_RATE_LIMIT` | 下载限速（字节/秒，0=不限） | `0` |

> **主密钥自动生成**：本地单用户使用无需手工填写。首次运行会自动生成强随机
> 密钥并保存到 `~/.config/panbridge/secret.key`（权限 0600，已加入 `.gitignore`）。
> 多进程/多机共享时再通过环境变量显式注入。

## 夸克账号登录（扫码）

夸克通过扫码登录，无需手动获取 Cookie：

```bash
# 1. 扫码登录（终端显示二维码，夸克 App 扫描后自动完成）
uv run panbridge auth login

# 2. 查询登录状态
uv run panbridge auth status

# 3. 退出登录（删除本地账号凭据）
uv run panbridge auth logout
```

扫码成功后，Cookie 自动提取、验证并**加密持久化**到本地数据库，接入账号体系。
后续运行自动加载已登录账号，不再要求手动填写 Cookie。凭据失效时会提示重新扫码。

> 也提供等价 REST API（`POST /v1/auth/qr/start` 等，为 AstrBot 集成预留），
> 无需 WebUI，详见 [docs/api.md](docs/api.md)。

## 阿里云盘（解析无需登录）

阿里云盘的分享解析与文件预览**无需登录**，直接提交分享链接即可：

```bash
# 预览分享内容（无需账号）
curl -X POST http://127.0.0.1:8000/v1/shares/inspect \
  -H 'Content-Type: application/json' \
  -d '{"text": "https://www.alipan.com/s/MpYfsMSUdmk"}'
```

只有「转存到自己网盘」需要登录。阿里云盘当前支持手动提供
`refresh_token`（通过 `POST /v1/accounts` 加密存储），扫码登录待后续优化。

## API 示例

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 查看支持的平台
curl http://127.0.0.1:8000/v1/providers

# 解析分享（阿里云盘，无需登录）
curl -X POST http://127.0.0.1:8000/v1/shares/inspect \
  -H 'Content-Type: application/json' \
  -d '{"text": "https://www.alipan.com/s/MpYfsMSUdmk"}'

# 创建转存+下载任务
curl -X POST http://127.0.0.1:8000/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-unique-key' \
  -d '{"input": {"text": "https://pan.quark.cn/s/xxxx", "action": "save_and_download", "account_id": "acc1"}}'

# 查询任务（含 result：下载文件路径）
curl http://127.0.0.1:8000/v1/tasks/{task_id}

# 获取任务下载的文件列表 / 下载单个文件
curl http://127.0.0.1:8000/v1/tasks/{task_id}/files
curl -O http://127.0.0.1:8000/v1/tasks/{task_id}/files/{filename}
```

完整 API 合约见 [docs/api.md](docs/api.md)。

## 测试

```bash
uv run pytest
uv run ruff check src tests
```

### 真实夸克端到端测试（可选，需要真实账号）

先扫码登录，再一条命令跑通 `inspect → 转存 → 下载`，自动创建独立文件夹，不污染网盘根目录：

```bash
uv run panbridge auth login        # 终端扫码登录一次
uv run python scripts/e2e_quark.py --share 'https://pan.quark.cn/s/xxxx 提取码: xxxx'
```

- 不再需要 `.env.quark` / Cookie / 主密钥——脚本直接读取已登录账号。
- 默认自动创建独立文件夹转存，只转存分享中的**第一个非目录文件**，不删除/覆盖账号原有内容。
- 凭据仅在内存中解密使用，不打印到日志。

详见 [docs/providers.md](docs/providers.md)。

## 文档

- [架构设计](docs/architecture.md)
- [API 合约](docs/api.md)
- [Provider 说明与复用来源](docs/providers.md)
- [运维部署](docs/operations.md)

## 许可证

MIT
