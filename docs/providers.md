# Provider 说明与复用来源

## 现状

已实现三个 Provider：

| Provider | 类型 | 用途 |
|----------|------|------|
| `mock` | Mock | 无账号环境验证完整任务链路（测试/演示） |
| `quark` | 真实接入 | 夸克网盘转存与下载（基于 QuarkPan） |
| `aliyun` | 真实接入 | 阿里云盘分享解析/下载（基于 aligo） |

## 参考项目评估

| 项目 | 许可证 | 复用方式 | 结论 |
|------|--------|----------|------|
| [QuarkPan](https://github.com/lich0821/QuarkPan) | MIT | **PyPI 依赖**（`quarkpan`） | ✅ **已采用**：通过 `pip install quarkpan` 引入，薄封装为 `QuarkProvider` |
| [aligo](https://github.com/foyoux/aligo) | MIT | **PyPI 依赖**（`aligo`） | ✅ **已采用**：复用 API 端点常量与 schema，薄封装为 `AliyunProvider` |
| [quark-auto-save](https://github.com/Cp0204/quark-auto-save) | AGPL-3.0 | 仅参考思路 | ❌ 不可复制代码（AGPL 传染性），仅参考 API 请求思路 |
| [cloud-auto-save](https://github.com/OzoO0/cloud-auto-save) | 待核实 | 待评估 | 未采用，需进一步核实许可证 |
| [pan123](https://github.com/jonntd/Pan123) | MIT | 待评估 | 123 云盘开放平台 SDK，需 access_token（开放平台资质），暂未接入 |
| [bypy](https://github.com/houtianze/bypy) | MIT | 待评估 | 百度网盘 PCS 客户端，仅能操作 `/apps/bypy` 目录，且已进入维护状态，不适合转存他人分享 |

## 夸克网盘真实接入（QuarkProvider）

### 依赖来源

- 包名：`quarkpan`（PyPI）
- 版本：`1.0.5`（已在 `requirements.txt` 锁定）
- 许可证：**MIT**
- 复用方式：作为**依赖引入**，通过薄封装实现 `ShareProvider` 接口，未复制源码。

### 复用的接口（已核对真实源码）

| 方法 | 用途 | 备注 |
|------|------|------|
| `APILogin.get_qr_code()` | 获取扫码登录二维码 token + URL | `uop.quark.cn/cas/ajax/getTokenForQrcodeLogin` |
| `APILogin.check_login_status(qr_token)` | 单次查询扫码状态 | `getServiceTicketByQrcodeToken`，返回 `service_ticket` |
| `QuarkClient(cookies=..., auto_login=False)` | 用已有 Cookie 初始化 | 扫码后换取的 Cookie |
| `client.shares.get_share_token(share_id, password)` | 获取分享 `stoken` | `share/sharepage/token` |
| `client.shares.get_share_info(share_id, token)` | 解析分享文件列表 | `share/sharepage/detail` |
| `client.shares.save_shared_files(...)` | 转存 | `share/sharepage/save`，返回 `data.task_id` |
| `client.get_download_url(file_id)` | 获取下载链接 | `file/download` |
| `client.list_files(folder_id)` | 列出账号文件 | `file/sort` |

### 扫码登录（复用与副作用隔离）

夸克通过**扫码登录**获取 Cookie，不需要手动获取 Cookie。认证流程复用
QuarkPan 的 `APILogin` 底层协议能力（`get_qr_code` + `check_login_status`），
但**明确隔离其副作用**：

| QuarkPan 原实现 | 副作用 | 本项目的处理 |
|-----------------|--------|--------------|
| `APILogin.login()` / `wait_for_login()` | `print`、倒计时线程、`time.sleep` 阻塞轮询 | **不复用**，改用 `asyncio.sleep` 异步轮询，不阻塞 HTTP |
| `APILogin._save_login_result()` | 写 `login_result.json` / `user_info.json` 到 QuarkPan 配置目录（明文凭据） | **不复用** |
| `APILogin._get_user_info_and_cookies()` | 用 service_ticket 请求 `/account/info` 换 Cookie，但写用户信息文件 | 等价网络行为自行实现（`AuthService._exchange_cookie`），**不写任何文件** |
| `QuarkAuth._save_cookies()` | 明文 Cookie 写 `cookies.json` | **不复用**，凭据加密后入库 |

换 Cookie 的流程：扫码成功 → `service_ticket` → 请求
`pan.quark.cn/account/info?st=<ticket>&lw=scan` → 从响应 Set-Cookie 提取
`quark.cn` 域名的 Cookie → 拼接为字符串 → **Fernet 加密**后存入
`accounts` 表（复用 `AccountRepository`）。明文 Cookie 只在内存中短暂存在。

### 字段映射（夸克 → 领域）

| 夸克字段 | 领域字段 |
|----------|----------|
| `fid` | `file_id` |
| `file_name` | `name` |
| `dir`（bool） | `is_dir` |
| `size` | `size` |

### 关键设计

1. **线程隔离**：QuarkPan 是同步 API，`QuarkProvider` 用 `asyncio.to_thread` 在线程池调用，避免阻塞 FastAPI 事件循环。
2. **凭据加密与解密**：夸克通过 Cookie 认证。账号 `encrypted_credential` 存的是 **Fernet 密文**，`QuarkProvider` 注入 `CredentialCipher`，在 `_client_for` 内部解密为明文 Cookie 使用。密文只在 Provider 内短暂解密，不泄漏到领域对象或 API 响应。
   - 主密钥通过 `PANBRIDGE_SECRET_KEY` 注入，**未配置时拒绝加密/解密**（不再使用不安全默认值）。
   - 解密失败（密钥不匹配）映射为 `ACCOUNT_EXPIRED`。
3. **账号校验**：`validate_account` 用账号 Cookie 实际调用 `get_storage_info`（capacity 接口）验证，而非 QuarkPan 的 `is_logged_in()`（后者检查本地 `cookies.json` 文件，与账号 Cookie 无关）。
4. **错误映射**：`AuthenticationError` → `ACCOUNT_EXPIRED`；404/不存在/失效 → `INVALID_SHARE`；空间/容量 → `QUOTA_EXCEEDED`；验证码/风控 → `ACTION_REQUIRED`；密码/提取码 → `INVALID_PASSWORD`。
5. **转存后文件 ID 衔接**：夸克转存是异步任务，文件在账号网盘获得**新 fid**（与分享源 fid 不同）。`save_shared_files` 只返回 `task_id`（用于幂等确认），不返回新 fid。`save_and_download` 流程通过 `resolve_saved_file_ids` 从目标目录按文件名重新查询新 fid 后再下载。**该字段结构需真实账号验证**。
   - **同名误匹配防护**：若目标目录中某个源文件名匹配到**多个**文件，`resolve_saved_file_ids` 会明确抛出 `INTERNAL_ERROR`（ambiguous），**不会静默选错**。配合「转存到独立空文件夹」可从根本上避免同名冲突。
6. **转存目标目录（create_folder）**：`QuarkProvider` 实现 `create_folder`（调用夸克 `file` 接口，返回新目录 fid）。当任务未指定 `destination` 时，Worker 自动创建 `panbridge-<task前缀>` 独立文件夹，转存到该文件夹内，**避免污染根目录、避免与账号现有同名文件冲突**。若平台不支持创建文件夹（`UNSUPPORTED_OPERATION`），则回退到根目录 `"0"` 并明确记录。
7. **纯下载的限制**：夸克**不支持不转存直接下载分享源文件**——`get_download_url`（`file/download`）需要的是**账号网盘里的 fid**，而非分享源 fid。因此 `DOWNLOAD` 动作对夸克通常不可用，应使用 `save_and_download`（先转存再下载）。
8. **下载 headers**：夸克 CDN 可能校验 `Referer`，`get_download_info` 返回下载所需 headers（`Referer: https://pan.quark.cn/` + 常见 `User-Agent`），Worker 下载时透传。**不含 Cookie**（下载 URL 已带签名，且凭据不得外泄）。
9. **destination 语义**：`destination` 是夸克目标目录的 **fid**（`"0"` 表示根目录），不是目录名。为空时自动创建独立文件夹（见第 6 点）。
10. **不伪造**：所有请求字段以 QuarkPan 源码为准（已逐条核对 `share_service.py`、`file_service.py`、`file_download_service.py`、`client.py`、`auth/login.py`、`cli/commands/basic_fileops.py`）。

### 已验证情况（诚实声明）

- ✅ **已离线验证**：字段映射、错误映射、链接识别（`pan.quark.cn`）、凭据加解密往返、密钥不匹配映射——通过单元测试覆盖。
- ✅ **真实网络可达**：不存在的分享返回 404 被正确映射为 `INVALID_SHARE`（网络可达）。
- ⚠️ **未经真实账号验证**：当前无真实夸克账号 Cookie，转存、转存后新 fid 查询、真实下载链接均未做端到端验证。接入前需按下方「人工验证步骤」操作，**不要将离线验证等同于真实网盘测试通过**。

### 人工验证步骤（有真实账号时）

**推荐：扫码登录 + 本地端到端脚本**（自动完成 `inspect → save_and_download`，不打印 Cookie）：

```bash
# 1. 终端扫码登录一次（自动保存账号，无需手动获取 Cookie）
uv run panbridge auth login

# 2. 运行端到端脚本（自动读取已登录账号）
uv run python scripts/e2e_quark.py --share 'https://pan.quark.cn/s/xxxx 提取码: xxxx'
```

脚本会依次打印：解析分享 → 转存下载 → 本地文件路径与大小。
它只转存分享中的**第一个非目录文件**（小文件），转存到自动创建的独立文件夹，
不删除或覆盖账号原有文件。

**手动 API 方式**（等价，为 AstrBot 集成预留）：

1. 发起扫码登录，获取二维码 URL：
   ```bash
   curl -X POST http://127.0.0.1:8000/v1/auth/qr/start
   ```
2. 用手机夸克 App 扫描返回的 `qr_url` 对应二维码。
3. 轮询登录状态（会自动完成换 Cookie + 加密入库）：
   ```bash
   curl http://127.0.0.1:8000/v1/auth/qr/{session_id}
   ```
4. 查询登录状态 / 退出登录：
   ```bash
   curl http://127.0.0.1:8000/v1/auth/status
   curl -X POST http://127.0.0.1:8000/v1/auth/logout
   ```
5. 转存 + 下载（用登录返回的账号 ID）：
   ```bash
   curl -X POST http://127.0.0.1:8000/v1/tasks \
     -H 'Content-Type: application/json' \
     -d '{"input": {"text": "https://pan.quark.cn/s/xxxx", "action": "save_and_download", "account_id": "<账号id>"}}'
   ```

## 阿里云盘真实接入（AliyunProvider）

### 依赖来源

- 包名：`aligo`（PyPI）
- 版本：`6.2.8`（在 `pyproject.toml` 声明 `aligo>=6.0`）
- 许可证：**MIT**
- 复用方式：复用 aligo 定义的 **API 端点常量与请求/响应 schema**，但用**独立 httpx 客户端**发起匿名请求（见下「副作用隔离」），转存路径复用 `Aligo(refresh_token=...)`。

### 核心特性：分享解析/下载无需登录

已用真实公开分享链接验证（无凭据）：

| 能力 | 端点 | 是否需登录 | 验证状态 |
|------|------|-----------|---------|
| 获取分享 token | `POST /v2/share_link/get_share_token` | 否 | ✅ 真实验证 |
| 分享详情（标题/文件数） | `POST /adrive/v2/share_link/get_share_by_anonymous` | 否 | ✅ 真实验证 |
| 分享文件列表 | `POST /adrive/v2/file/list_by_share` | 否 | ✅ 真实验证 |
| 分享直链下载 | `POST /v2/file/get_share_link_download_url` | 否 | ⚠️ 未验证 |
| 转存到自己网盘 | `share_file_saveto_drive` | **是**（refresh_token） | ⚠️ 需账号 |
| 自己盘文件直链 | `get_download_url` | 是 | ⚠️ 需账号 |

因此 `inspect_share`、`list_share_files` 无需账号即可工作；只有
`save_shared_files`（转存）需要登录。

### 副作用隔离

aligo 的 `Aligo()` / `Auth()` 在初始化时会**阻塞轮询二维码、打印终端 ASCII
二维码、写 `~/.aligo/` 配置目录**。本项目：

- 匿名请求（分享解析/列表/直链）**完全不用 aligo 的客户端**，改用独立
  `httpx.AsyncClient` 直接调用公开 API（复用 aligo 的端点常量），无任何副作用。
- 转存路径复用 `Aligo(refresh_token=...)`，但通过 `show=lambda _: None`
  静默二维码、`level=logging.ERROR` 抑制终端日志、`re_login=False` 避免
  token 失效时阻塞重新登录。

### 字段映射（阿里云盘 → 领域）

| 阿里字段 | 领域字段 |
|----------|----------|
| `file_id` | `file_id` |
| `name` | `name` |
| `type`（`file`/`folder`） | `is_dir` |
| `size` | `size` |

### 登录方式

阿里云盘通过 **refresh_token** 认证（OAuth）。当前支持手动提供
refresh_token（通过 `POST /v1/accounts`，凭据加密存储）；扫码登录
（aligo 的 `Auth` 二维码流程）作为后续优化，未实现。

### 已验证情况（诚实声明）

- ✅ **真实验证**：匿名分享解析（token 获取、详情、文件列表）用真实公开
  分享链接跑通；已取消分享正确映射为 `INVALID_SHARE`。
- ✅ **离线验证**：字段映射、错误映射、链接识别（`alipan.com`/`aliyundrive.com`）
  通过单元测试覆盖。
- ⚠️ **未经真实账号验证**：转存、分享直链下载、自己盘下载、refresh_token
  登录均需真实账号，未做端到端验证。

## 新增 Provider 的规范

每新增一个 Provider 必须：

1. 实现统一能力声明（`capabilities`）
2. 错误映射为统一 `DomainError` 错误码
3. 提供可离线运行的 Mock 测试
4. 在 `docs/providers.md` 记录依赖来源、版本、复用方式、许可证和已验证能力
5. 平台协议变化时只改对应 Adapter，不改外部 API
