# API 合约

所有公开接口使用 `/v1` 前缀。默认监听 `127.0.0.1:8000`。

## 鉴权

通过 `X-API-Key` 请求头鉴权（当 `PANBRIDGE_API_KEY` 已配置时）。未配置时本机免鉴权（仅限开发）。

## 错误结构

统一错误响应：

```json
{
  "code": "INVALID_SHARE",
  "message": "share does not exist",
  "retryable": false,
  "details": {},
  "request_id": "abc123"
}
```

错误码：

| code | 说明 |
|------|------|
| `INVALID_SHARE` | 分享链接无效或无法识别 |
| `INVALID_PASSWORD` | 提取码错误 |
| `INVALID_REQUEST` | 请求参数错误 |
| `UNSUPPORTED_PROVIDER` | 不支持的网盘平台 |
| `UNSUPPORTED_OPERATION` | 平台不支持该操作 |
| `ACCOUNT_EXPIRED` | 账号凭据过期 |
| `ACCOUNT_NOT_FOUND` | 账号不存在 |
| `ACTION_REQUIRED` | 需要额外操作（如重新授权） |
| `QUOTA_EXCEEDED` | 空间不足 |
| `RATE_LIMITED` | 触发限流 |
| `DOWNLOAD_FAILED` | 下载失败 |
| `TASK_NOT_FOUND` | 任务不存在 |
| `TASK_NOT_CANCELLABLE` / `TASK_NOT_RETRYABLE` | 状态不允许该操作 |
| `IDEMPOTENCY_CONFLICT` | 幂等键冲突 |
| `UNAUTHORIZED` | 鉴权失败 |
| `INTERNAL_ERROR` | 内部错误 |

## 端点

### `GET /health`

健康检查，无需鉴权。

### `GET /v1/providers`

列出已注册的网盘平台及其能力。

### `POST /v1/shares/inspect`

解析分享，预览文件列表。

请求：
```json
{ "text": "https://pan.quark.cn/s/abc123 提取码: 1234" }
```

响应：
```json
{
  "provider": "mock",
  "share_id": "mock001",
  "title": "mock-share-mock001",
  "files": [
    { "file_id": "f1", "name": "video.mkv", "is_dir": false, "size": 1024 }
  ]
}
```

### `POST /v1/tasks`

创建任务，返回 `202 Accepted` 与任务 ID。

请求体：
```json
{
  "input": {
    "text": "https://pan.quark.cn/s/abc123",
    "action": "save",
    "account_id": "acc1",
    "selection": ["f1", "f2"],
    "destination": "/my/folder",
    "on_conflict": "skip"
  }
}
```

- `action`：`inspect` | `save` | `download` | `save_and_download`
- `selection`：指定文件 ID 列表；缺省则转存全部非目录文件
- `on_conflict`：`skip` | `rename` | `overwrite`
- 支持 `Idempotency-Key` 请求头：相同 key + 相同请求体返回已有任务；相同 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`

### `GET /v1/tasks`

任务列表，支持 `limit` / `offset`。

### `GET /v1/tasks/{task_id}`

查询单个任务状态与结果。任务完成后 `result` 字段包含下载文件列表
（`files: [{name, path, size}]`）或转存结果（`saved`、`destination`）。

### `GET /v1/tasks/{task_id}/files`

返回任务下载产出的文件列表（来自任务 `result.files`）。

### `GET /v1/tasks/{task_id}/files/{filename}`

下载任务产出的本地文件。文件名必须出现在任务 `result.files` 中，
且文件路径受限于 `download_dir` 内（防止路径穿越）。

### `POST /v1/tasks/{task_id}/cancel`

取消任务。

### `POST /v1/tasks/{task_id}/retry`

重试失败/等待授权的任务。

### `GET /v1/accounts` / `POST /v1/accounts`

账号列表 / 添加账号。账号凭据（Cookie）加密存储，接口不返回明文。

> 推荐改用扫码登录（见下方 `/v1/auth/*`），无需手动获取 Cookie 或配置主密钥。

## 认证（扫码登录）

本地单用户首次运行会自动生成加密主密钥并持久化，无需手工填写。

### `POST /v1/auth/qr/start`

发起扫码登录，返回二维码 URL。

请求：`?provider=quark`（默认）

响应：
```json
{ "session_id": "abc...", "provider": "quark", "qr_url": "https://su.quark.cn/..." }
```

### `GET /v1/auth/qr/{session_id}`

轮询扫码状态。`status`：`waiting`（待扫码）→ `success`（已登录）→ `expired`（过期）。

扫码成功时会自动完成换 Cookie、加密持久化，并返回账号信息。

### `GET /v1/auth/status`

查询当前登录状态：

```json
{ "logged_in": true, "expired": false, "account": { "id": "...", "provider": "quark", "label": "..." } }
```

凭据失效时 `expired=true`，应提示重新扫码（不会静默使用失效 Cookie）。

### `POST /v1/auth/logout`

退出登录，删除本地保存的账号凭据。

## 任务状态

`queued` → `resolving` → `saving` / `downloading` → `completed` / `failed` / `cancelled`

中间态还包括 `waiting_auth`、`retrying`。
