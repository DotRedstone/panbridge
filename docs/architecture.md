# 架构设计

## 总览

PanBridge 采用**模块化单体**架构，明确分层，避免微服务与复杂事件总线的开销。

```
┌─────────────────────────────────────────────┐
│                  API Layer                  │
│   HTTP API / 鉴权 / 校验 / 错误响应 / 查询   │
└──────────────────┬──────────────────────────┘
                   │ 依赖
┌──────────────────▼──────────────────────────┐
│             Application Layer               │
│   ParseShare / Inspect / Save / Download /  │
│   CreateTask / Cancel / Retry / Accounts    │
└──────┬───────────────┬──────────────┬───────┘
       │               │              │
┌──────▼─────┐  ┌──────▼──────┐  ┌───▼────────┐
│  Provider  │  │  Download   │  │ Persistence│
│  Layer     │  │  Layer      │  │   Layer    │
└────────────┘  └─────────────┘  └────────────┘
       │
┌──────▼──────────────────────────────────────┐
│              Domain Layer (无框架依赖)       │
│  Provider / Account / Share / Task / Error  │
└─────────────────────────────────────────────┘
```

## 各层职责

### Domain Layer（`domain/`）

独立于框架的核心领域对象与错误。不依赖 FastAPI、SQLAlchemy 或具体网盘 SDK。

- `Provider`、`Capability`、`Account`、`Share`、`ShareFile`、`Task`、`TransferResult`、`DownloadArtifact`
- `DomainError` + 稳定错误码 `ErrorCode`
- 枚举：`Action`、`TaskStatus`、`OnConflict`、`Capability`

### Application Layer（`application/`）

业务用例编排，不包含具体网盘 HTTP 请求细节。

- `link_extraction.py`：从文本提取链接与提取码、识别平台
- `service.py`：`ApplicationService` 提供 inspect/create/query/cancel/retry/accounts 用例

### Provider Layer（`providers/`）

将不同网盘映射为统一能力。

- `base.py`：`ShareProvider` 抽象接口
- `registry.py`：`ProviderRegistry` 注册表
- `quark/`：夸克网盘目录（当前含 `mock.py`，真实 Adapter 在 M1 实现）

每个平台用明确的 `Capability` 声明能力，不支持的操作抛 `UNSUPPORTED_OPERATION`。

### Task Engine（`workers/`）

耗时业务通过持久化后台任务执行。

- API 只负责创建任务，独立 Worker 执行
- 任务持久化到数据库，不依赖 `asyncio.create_task()`
- 状态机：`queued → resolving → saving/downloading → completed/failed`（含 `waiting_auth`、`retrying`、`cancelled`）
- 单 Worker 通过**数据库租约 + 心跳**识别中断任务，支持异常重启恢复
- 幂等：通过 `Idempotency-Key` + 远端任务标识降低重复转存风险（不承诺远端不存在的 exactly-once）

### Download Layer（`downloads/`）

独立下载接口，与具体网盘解耦。

- `base.py`：`Downloader` 抽象
- `http.py`：内置 HTTP 流式下载（临时文件 + 原子完成、断点续传、超时、大小限制）
- `security.py`：SSRF 防护（URL 校验、拒绝内网/回环/元数据地址）

## 依赖方向

单向依赖，禁止反向：

```
domain ← application ← api
        ← providers
        ← downloads
        ← persistence
```

## 关键决策

1. **单 Worker 可靠执行**：SQLite 下先用租约机制，避免不可靠的多进程并发抢占。PostgreSQL 下可扩展为多 Worker。
2. **转存幂等**：记录远端任务标识 + 目标文件检查，降低「转存成功但记录崩溃」导致的重复转存。
3. **凭据加密**：Fernet 对称加密，主密钥通过 `PANBRIDGE_SECRET_KEY` 外部注入。
4. **下载安全**：下载 CDN 域名不套用分享域名白名单，而是通过可信 Provider 产生下载信息 + 独立 SSRF 校验。
