# PanBridge

多网盘自动转存与下载后端服务。用户通过 HTTP API 提交网盘分享链接，服务自动识别平台、解析分享内容，并使用已配置的网盘账号完成转存与下载。

## 支持平台

| 平台 | 登录方式 | 能力 | 验证状态 |
|------|---------|------|---------|
| 夸克网盘 | 扫码登录 | 解析 / 转存 / 下载 | ✅ 匿名解析已真实验证 |
| 阿里云盘 | 扫码登录（refresh_token） | 解析 / 预览（**无需登录**）/ 转存 / 下载 | ✅ 解析已真实验证 |
| 蓝奏云 | 无需登录 | 解析 / 直链下载（**匿名**） | ✅ 解析+直链真实验证（下载节点验证码风控） |
| 123 云盘 | 扫码登录（token） | 解析 / 直链下载（**匿名**）/ 转存 / 登录态下载 | ✅ 匿名解析 + 登录态下载协议真实验证 |
| 百度网盘 | BDUSS Cookie | 解析列表 / 直链 / 转存 | 🟠 匿名触发风控（需 Cookie） |
| 115 网盘 | 扫码登录（Cookie） | 解析（**匿名**）/ 转存 / 下载 / 文件列表 | ⚠️ 匿名解析 + Mock |
| PikPak | 无需登录（分享） | 解析 / 直链下载（**匿名**）/ 转存 | ⚠️ 协议实现 + Mock |
| 天翼云盘 | **账号密码登录** | 分享解析（**匿名**）/ 转存 / 下载 / 账号文件列表（**需登录**） | ⚠️ 协议 + Mock |
| 移动云盘（和彩云） | Authorization(Basic) | 分享解析 / 转存 / 账号文件列表 / 下载（**需登录**） | ⚠️ 协议 + Mock |
| UC 网盘 | **扫码登录**（Cookie） | 分享解析 / 转存 / 下载 / 账号文件列表（**需登录**） | ⚠️ 扫码端点真实调通 |
| 迅雷云盘 | refresh_token | 分享解析 / 转存 / 下载 / 账号文件列表（**需登录**） | ⚠️ 协议 + Mock |
| 腾讯微云 | skey Cookie | 分享解析（**匿名**）/ 转存（**需登录**） | ⚠️ 协议 + Mock |
| 光鸭云盘 | access_token/refresh_token | 分享解析（**匿名**）/ 转存 / 下载 / 文件列表（**需登录**） | ⚠️ 协议 + Mock |
| 悟空网盘 | Cookie/token | 分享解析（**匿名**）/ 转存 / 下载 / 文件列表（**需登录**） | ⚠️ 协议 + Mock |
| Mock | 无需 | 全链路（测试/演示） | ✅ 完整可用 |

> 说明：
> - **蓝奏云 / 123 / PikPak / 百度（列表）/ 115（列表）/ 阿里云盘 / 微云 / 光鸭 / 悟空 / 天翼**的
>   「解析分享、预览文件列表」**无需登录**即可工作。
> - **UC / 迅雷 / 移动云盘**的分享解析与转存**需要登录态**，纯预览会返回
>   `ACTION_REQUIRED`，转存/下载流程会使用账号凭据自动完成。
> - **匿名直链下载**：仅 aliyun/lanzou/123/pikpak 四个平台支持（无需账号直接下载分享）。
>   其余平台分享下载需登录态，走「先转存再下载」。
> - 转存与下载大多需要对应账号（Cookie/refresh_token/access_token）。
> - 各平台能力边界与验证状态见 [docs/providers.md](docs/providers.md)。

## 特性

- 自动识别网盘链接与提取码（夸克/阿里/123/UC/百度/115/迅雷/蓝奏云等域名）
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
| `PANBRIDGE_MAX_DOWNLOAD_BYTES` | 单文件下载量上限（字节，0=不限） | `0` |
| `PANBRIDGE_ALLOW_INSECURE_DOWNLOAD` | 允许下载回环地址（仅本地集成测试） | 关 |

> **主密钥自动生成**：本地单用户使用无需手工填写。首次运行会自动生成强随机
> 密钥并保存到 `~/.config/panbridge/secret.key`（权限 0600，已加入 `.gitignore`）。
> 多进程/多机共享时再通过环境变量显式注入。

## 账号登录（扫码 + 账号密码）

**扫码登录支持夸克、阿里云盘、115 网盘、123 云盘、UC 网盘**（复用各平台
SDK 的官方二维码协议，凭据加密入库，不落 SDK 明文目录）：

```bash
# 1. 扫码登录（终端显示二维码，用对应 App 扫描后自动完成）
uv run panbridge auth login --provider quark     # 夸克 App
uv run panbridge auth login --provider aliyun    # 阿里云盘 App
uv run panbridge auth login --provider 115       # 115 App
uv run panbridge auth login --provider 123       # 123 云盘 App
uv run panbridge auth login --provider uc        # UC 网盘 App

# 1b. 账号密码登录（天翼云盘；扫码接口已失效，账号密码更可靠）
uv run panbridge auth login-password --provider cloud189

# 2. 查询登录状态
uv run panbridge auth status --provider quark

# 3. 退出登录（删除本地账号凭据）
uv run panbridge auth logout --provider quark
```

扫码成功后，凭据（Cookie / refresh_token）自动提取、验证并**加密持久化**
到本地数据库。后续运行自动加载已登录账号，不再要求手动填写。凭据失效时
会提示重新扫码。

> **其他平台的认证方式**：百度（BDUSS Cookie）、移动云盘（Authorization）、
> 迅雷（refresh_token）、微云（skey）、光鸭（access_token）、悟空（Cookie/token）
> 目前**没有扫码登录**，通过手工凭据导入（加密入库，不落明文）：
>
> ```bash
> uv run panbridge auth import --provider baidu --credential "BDUSS=..."
> ```
>
> 不要把需要手工 Cookie/token 的平台描述为「扫码即可使用」。

> 也提供等价 REST API（`POST /v1/auth/qr/start` 等），无需 WebUI，
> 详见 [docs/api.md](docs/api.md)。

## AstrBot 机器人集成

内置 AstrBot 插件（见 [`astrbot-plugin/`](astrbot-plugin/)），可让机器人
在 QQ / 微信 / Telegram / 钉钉 / 飞书等平台调用 PanBridge 服务：

- 把 `astrbot-plugin/` 目录放入 AstrBot 的 `data/plugins/`，在管理面板启用并
  配置 `base_url`（PanBridge 服务地址）与 `api_key`。
- 在机器人对话中直接发送网盘链接即自动解析，或用指令转存/下载/扫码登录：

```
/pb <链接>             解析分享
/pb download <链接>    下载
/pb save <链接>        转存
/pb status [id]        查任务
/pb login <provider>   扫码登录（发二维码图片）
```

详见 [astrbot-plugin/README.md](astrbot-plugin/README.md)。

## 阿里云盘（解析无需登录）

阿里云盘的分享解析与文件预览**无需登录**，直接提交分享链接即可：

```bash
# 预览分享内容（无需账号）
curl -X POST http://127.0.0.1:8000/v1/shares/inspect \
  -H 'Content-Type: application/json' \
  -d '{"text": "https://www.alipan.com/s/MpYfsMSUdmk"}'
```

只有「转存到自己网盘」需要登录。阿里云盘支持扫码登录
（`panbridge auth login --provider aliyun`）或手动导入 refresh_token
（`panbridge auth import --provider aliyun --credential "<refresh_token>"`）。

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
