# Provider 说明与复用来源

## 现状

已实现两个 Provider：

| Provider | 类型 | 用途 |
|----------|------|------|
| `mock` | Mock | 无账号环境验证完整任务链路（测试/演示） |
| `quark` | 真实接入 | 夸克网盘转存与下载（基于 QuarkPan） |

## 参考项目评估

| 项目 | 许可证 | 复用方式 | 结论 |
|------|--------|----------|------|
| [QuarkPan](https://github.com/lich0821/QuarkPan) | MIT | **PyPI 依赖**（`quarkpan`） | ✅ **已采用**：通过 `pip install quarkpan` 引入，薄封装为 `QuarkProvider` |
| [quark-auto-save](https://github.com/Cp0204/quark-auto-save) | AGPL-3.0 | 仅参考思路 | ❌ 不可复制代码（AGPL 传染性），仅参考 API 请求思路 |
| [cloud-auto-save](https://github.com/OzoO0/cloud-auto-save) | 待核实 | 待评估 | 未采用，需进一步核实许可证 |

## 夸克网盘真实接入（QuarkProvider）

### 依赖来源

- 包名：`quarkpan`（PyPI）
- 版本：`1.0.5`（已在 `requirements.txt` 锁定）
- 许可证：**MIT**
- 复用方式：作为**依赖引入**，通过薄封装实现 `ShareProvider` 接口，未复制源码。

### 复用的接口（已核对真实源码）

| 方法 | 用途 | 备注 |
|------|------|------|
| `QuarkClient(cookies=..., auto_login=False)` | 用已有 Cookie 初始化 | 避免二维码登录 |
| `client.shares.get_share_token(share_id, password)` | 获取分享 `stoken` | `share/sharepage/token` |
| `client.shares.get_share_info(share_id, token)` | 解析分享文件列表 | `share/sharepage/detail` |
| `client.shares.save_shared_files(...)` | 转存 | `share/sharepage/save`，返回 `data.task_id` |
| `client.get_download_url(file_id)` | 获取下载链接 | `file/download` |
| `client.list_files(folder_id)` | 列出账号文件 | `file/sort` |

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
6. **不伪造**：所有请求字段以 QuarkPan 源码为准（已逐条核对 `share_service.py`、`file_service.py`、`file_download_service.py`、`client.py`、`auth/login.py`）。

### 已验证情况（诚实声明）

- ✅ **已离线验证**：字段映射、错误映射、链接识别（`pan.quark.cn`）、凭据加解密往返、密钥不匹配映射——通过单元测试覆盖。
- ✅ **真实网络可达**：不存在的分享返回 404 被正确映射为 `INVALID_SHARE`（网络可达）。
- ⚠️ **未经真实账号验证**：当前无真实夸克账号 Cookie，转存、转存后新 fid 查询、真实下载链接均未做端到端验证。接入前需按下方「人工验证步骤」操作，**不要将离线验证等同于真实网盘测试通过**。

### 人工验证步骤（有真实账号时）

0. 生成并注入主密钥：
   ```bash
   export PANBRIDGE_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
   ```
1. 获取夸克 Cookie（浏览器登录 `pan.quark.cn` 后从开发者工具复制 Cookie）。
2. 通过 API 添加账号（Cookie 会被加密存储）：
   ```bash
   curl -X POST http://127.0.0.1:8000/v1/accounts \
     -H 'Content-Type: application/json' \
     -d '{"provider": "quark", "label": "my-quark", "credential": "<你的Cookie>"}'
   ```
3. 解析一个真实分享：
   ```bash
   curl -X POST http://127.0.0.1:8000/v1/shares/inspect \
     -H 'Content-Type: application/json' \
     -d '{"text": "https://pan.quark.cn/s/xxxx 提取码: xxxx"}'
   ```
4. 转存 + 下载：
   ```bash
   curl -X POST http://127.0.0.1:8000/v1/tasks \
     -H 'Content-Type: application/json' \
     -d '{"input": {"text": "https://pan.quark.cn/s/xxxx", "action": "save_and_download", "account_id": "<上一步返回的账号id>"}}'
   ```

## 新增 Provider 的规范

每新增一个 Provider 必须：

1. 实现统一能力声明（`capabilities`）
2. 错误映射为统一 `DomainError` 错误码
3. 提供可离线运行的 Mock 测试
4. 在 `docs/providers.md` 记录依赖来源、版本、复用方式、许可证和已验证能力
5. 平台协议变化时只改对应 Adapter，不改外部 API
