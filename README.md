# PanBridge

多网盘自动转存与下载后端服务。用户通过 HTTP API 提交网盘分享链接，服务自动识别平台、解析分享内容，并使用已配置的网盘账号完成转存与下载。

第一版优先支持**夸克网盘**，已基于 [QuarkPan](https://github.com/lich0821/QuarkPan)（MIT）实现真实接入；同时提供完整可运行的 **Mock Provider**，可在无真实账号环境下验证任务生命周期。

## 特性

- 自动识别网盘链接与提取码
- 解析分享文件与目录
- 使用已授权账号转存文件
- 下载文件到本地（内置 HTTP 流式下载）
- 持久化后台任务，支持状态查询、失败原因、取消与重试
- 统一 REST API（`/v1` 前缀），OpenAPI 文档
- 不同网盘使用独立 Adapter，便于扩展
- 账号凭据本地加密存储，不写死在代码中

## 快速开始

### 依赖

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

### 安装与运行

```bash
# 使用 uv（推荐）
uv sync

# 启动 API 服务（默认监听 127.0.0.1:8000）
uv run uvicorn panbridge.main:app --host 127.0.0.1 --port 8000

# 另开终端启动 Worker（后台任务执行）
uv run python -m panbridge.worker
```

### 使用 Nix 开发环境

```bash
nix develop
```

### 配置

通过环境变量配置，参见 `.env.example`。关键项：

| 变量 | 说明 | 默认 |
|------|------|------|
| `PANBRIDGE_API_KEY` | API 鉴权密钥（为空则本机免鉴权） | 空 |
| `PANBRIDGE_SECRET_KEY` | 凭据加密主密钥（**必需**，添加账号/解密 Cookie 用） | 无 |
| `PANBRIDGE_DATABASE_URL` | 数据库连接 | `sqlite+aiosqlite:///./panbridge.db` |
| `PANBRIDGE_DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `PANBRIDGE_BIND_HOST` / `PANBRIDGE_BIND_PORT` | 监听地址 | `127.0.0.1` / `8000` |

生成主密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 配置夸克账号

夸克通过 Cookie 认证。添加账号时 Cookie 会被加密存储，接口不返回明文：

```bash
curl -X POST http://127.0.0.1:8000/v1/accounts \
  -H 'Content-Type: application/json' \
  -d '{"provider": "quark", "label": "my-quark", "credential": "<你的Cookie>"}'
```

完整说明与人工验证步骤见 [docs/providers.md](docs/providers.md)。

## API 示例

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 解析分享（Mock Provider）
curl -X POST http://127.0.0.1:8000/v1/shares/inspect \
  -H 'Content-Type: application/json' \
  -d '{"text": "https://mock.local/s/mock001 提取码: 1234"}'

# 创建转存任务
curl -X POST http://127.0.0.1:8000/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-unique-key' \
  -d '{"input": {"text": "https://mock.local/s/mock001", "action": "save", "account_id": "acc1"}}'

# 查询任务
curl http://127.0.0.1:8000/v1/tasks/{task_id}
```

完整 API 合约见 [docs/api.md](docs/api.md)。

## 测试

```bash
uv run pytest
uv run ruff check src tests
```

### 真实夸克端到端测试（可选，需要真实账号）

本地一条命令跑通 `inspect → 转存 → 下载`，自动创建独立文件夹，不污染网盘根目录：

```bash
cp scripts/quark.env.example .env.quark   # 填入 Cookie、主密钥、有效分享链接
uv run python scripts/e2e_quark.py        # 一条命令完成端到端验证
```

- Cookie 只从本地 `.env.quark` 读取，**不会打印到日志**。
- 默认自动创建独立文件夹转存；也可在 `.env.quark` 指定已有空文件夹 `PANBRIDGE_QUARK_DEST_FID`。
- 只转存分享中的**第一个非目录文件**，不删除/覆盖账号原有内容。

详见 [docs/providers.md](docs/providers.md)。

## 文档

- [架构设计](docs/architecture.md)
- [API 合约](docs/api.md)
- [Provider 说明与复用来源](docs/providers.md)
- [运维部署](docs/operations.md)

## 许可证

MIT
