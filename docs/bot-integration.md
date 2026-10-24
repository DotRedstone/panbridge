# 统一 QQ 机器人接入契约（PanBridge 插件）

> 面向接入 PanBridge 插件到「统一 QQ 官方机器人」的场景。本文所有
> OpenID、群号、机器人 ID、地址均为**虚构样例**，不含任何真实部署值。
> 权限模型：**统一机器人业务主线确定用户群体与权限；插件只落实传入的
> 授权结果；PanBridge 后端只负责解析/转存/下载。** 插件不另建全局用户
> 身份库。

## 1. 角色与信任边界

```
统一 QQ 官方机器人（同一实例，多条业务线）
├── 作业插件等业务主线 —— 决定「谁是同学、谁是朋友、谁是管理员」
├── PanBridge 插件（astrbot-plugin/）—— 只落实授权结果，默认拒绝
└── PanBridge 后端（HTTP API）—— X-API-Key 服务间鉴权，无用户概念
```

信任原则：

- 可信身份**只**来自 AstrBot 事件对象上的平台侧字段（见 §3）；
- 昵称、消息正文中的自称（"我是群主"）、QQ 号文本、后端 `X-API-Key`
  **都不作为用户身份依据**；
- 授权配置缺失或身份不在白名单 → **默认拒绝**（fail-closed）。

## 2. 命令与能力清单

| 指令 | Capability | 默认 | 说明 |
|------|-----------|------|------|
| `/pb <链接>` | `panbridge.inspect` | 白名单用户 | 解析预览 |
| `/pb download <链接>` | `panbridge.download` | 白名单用户 | 创建下载任务 |
| `/pb save <链接>` | `panbridge.save` | **全局关闭**（`enable_save=false`） | 转存到共享账号；权限定义明确前不要开启 |
| `/pb status [id]` | `panbridge.task.view` | 白名单用户 | 缺省只列**自己的**任务 |
| `/pb cancel <id>` | `panbridge.task.cancel` | 白名单用户 | 仅自己的任务 |
| `/pb retry <id>` | `panbridge.task.retry` | 白名单用户 | 仅自己的任务 |
| `/pb login <平台>` | `panbridge.account.login` | **仅管理员** | 扫码登录网盘账号 |
| `/pb accounts` | `panbridge.account.list` | **仅管理员** | 列出凭据相关账号 |
| `/pb providers` | `panbridge.inspect` | 白名单用户 | 平台列表 |
| `/pb help` | `panbridge.inspect` | 白名单用户 | 帮助 |

## 3. 可信身份字段

插件从事件对象提取四元组（`astrbot-plugin/authz.py::TrustedIdentity`）：

| 字段 | 来源 | 虚构样例 | 说明 |
|------|------|---------|------|
| `platform` | `event.get_platform_id()` 的实例 ID | `course-qq-official` | 缺失时拒绝；平台类型名不能代替实例 ID |
| `bot_id` | 机器人实例（`bot_id` / `message_obj.self_id`） | `bot-001` | 同一平台可挂多个机器人；可用 `allowed_bots` 限定 |
| `openid` | 平台侧用户 OpenID（`get_sender_id()`） | `OPENID_ABC123` | QQ 官方平台为平台映射 ID，不是 QQ 号原文 |
| `scope` | 群 ID（`get_group_id()`）或私聊 | `group:G_10001` / `c2c` | 群聊场景需群白名单 |

身份匹配键 = `平台实例:OpenID`（如 `course-qq-official:OPENID_ABC123`）。
群白名单键 = `平台实例:群ID`（如 `course-qq-official:G_10001`）。

## 4. 授权接口

```python
# astrbot-plugin/authz.py（纯逻辑，可独立单测）
decision = authorizer.authorize(identity, capability, scene)

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str   # ok / no_config / bot_not_allowed /
                  # identity_not_authorized / group_not_allowed /
                  # admin_required / capability_disabled
```

判定顺序（任一步不通过即拒绝）：

1. 授权配置缺失（`admins`/`users` 全空）→ `no_config`，全拒绝；
2. 机器人实例不在 `allowed_bots` → `bot_not_allowed`；
3. 身份不在 `admins`/`users` → `identity_not_authorized`（默认拒绝）；
4. 群聊场景且群不在白名单 → `group_not_allowed`；
5. 凭据相关能力（login/accounts/logout）非管理员 → `admin_required`；
6. `save` 且全局开关关闭 → `capability_disabled`。

若业务主线未来提供在线授权服务，可在 `ConfigAuthorizer` 位置替换为
调用主线接口的实现（协议即上述 `authorize(identity, capability, scene)`），
插件其余代码不变。

## 5. HTTP 认证（插件 ↔ 后端）

- 服务间通道鉴权：`X-API-Key: <PANBRIDGE_API_KEY>`（对应后端
  `PANBRIDGE_API_KEY` 环境变量）；
- 该密钥是**服务间凭据，不是用户身份**——拿到后端地址 + key 不等于获得
  网盘功能，用户级权限全部在插件层判定；
- 后端默认监听 `127.0.0.1`；跨机部署时用 `PANBRIDGE_BIND_HOST=0.0.0.0`
  + 强随机 key，并建议叠加网络层隔离（内网/防火墙）。

## 6. 异步任务与回执

```
用户 → /pb download <链接> ──→ POST /v1/tasks（202，返回 task_id）
                              └─ 插件登记 task_id → 发起者身份键
用户 → /pb status <id> ──→ 归属校验（创建者或管理员）→ GET /v1/tasks/{id}
扫码 → /pb login <平台> ──→ POST /v1/auth/qr/start（仅管理员）
       后台轮询 GET /v1/auth/qr/{session_id}（每 2s，至多 5 分钟）
       回执 event.send 只发到**发起登录的那个会话**
```

- 任务归属登记在插件内存中；插件重启后登记丢失，存量任务仅管理员可
  查询/操作（fail-closed）；
- 同一身份同时只允许一个扫码会话；
- 后端 `GET /v1/tasks` 返回全局列表，**仅管理员**在 `/pb status` 缺省
  场景可见；普通用户永远走自己的归属记录。

## 7. QQ 官方平台限制（接入前必须知晓）

- **群聊**：QQ 官方平台机器人通常只能收到 @ 消息——同学在群里 @ 机器人
  发 `/pb ...`，未授权仍会被拒绝；被动识别开启时同样先过授权检查；
- **私聊（C2C）**：官方平台单聊消息有主动消息频控与列表限制，回执依赖
  被动回复窗口（约 5 分钟）；
- **后台回复**（`event.send` 主动推送，如扫码登录回调）：官方平台主动
  消息有配额，可能发送失败或延迟——登录结果若推送失败，用户可重新
  `/pb login` 查看会话状态；接入后须真机验证该路径；
- **图片/文件发送**：QQ 官方平台对媒体消息有审核与格式限制（二维码图片
  走 URL 引用）；**插件不向群里投递下载文件**，产出文件通过后端
  `GET /v1/tasks/{id}/files/{name}`（带鉴权）获取，投递方式由接入方决定。

## 8. 配置项汇总（虚构样例）

插件侧（AstrBot 插件配置）：

```yaml
base_url: "http://127.0.0.1:8000"
api_key: "<PANBRIDGE_API_KEY>"        # 服务间通道
default_provider: "quark"
# 授权（全部虚构；身份键 = 平台实例:OpenID）
admins: ["course-qq-official:OPENID_ABC123"]
users:  ["course-qq-official:OPENID_DEF456", "course-qq-official:OPENID_GHI789"]
groups: ["course-qq-official:G_10001"]       # 空 = 仅私聊
allowed_bots: ["bot-001"]             # 空 = 不限制
enable_save: false                    # 权限定义明确前保持 false
auto_detect_links: false              # 直接发链接默认关闭
```

后端侧（环境变量，见 `.env.example`）：`PANBRIDGE_API_KEY`（必配）、
`PANBRIDGE_SECRET_KEY`（多机共享时显式注入）、`PANBRIDGE_BIND_HOST/PORT`、
`PANBRIDGE_DOWNLOAD_DIR` 等。

## 9. 失败行为

| 情形 | 行为 |
|------|------|
| 未授权用户发 `/pb ...` | 统一提示「PanBridge 未对你的账号开放」（不区分原因，防探测） |
| 未授权/未配置时直接发链接 | **静默忽略**，不产出消息、不拦截事件 |
| 非网盘链接 / 其他插件命令 | 不响应、不拦截（作业插件等不受影响） |
| 非创建者查/取消/重试任务 | 「没有这个任务的查看/操作权限」（不泄露任务是否存在） |
| 普通用户 `/pb login`/`/pb accounts` | 同统一拒绝提示 |
| 后端不可达 | 提示「连接 PanBridge 服务失败」 |
| 授权配置缺失 | 所有人拒绝（`no_config`） |

## 10. 部署步骤（虚构样例）

1. 后端：设置 `PANBRIDGE_API_KEY`（强随机），保持 `127.0.0.1` 监听或内网隔离；
2. 插件：复制 `astrbot-plugin/` 到 AstrBot 插件目录，启用并按 §8 配置；
3. 把自己的身份键（平台实例 + OpenID，可先临时在群里让机器人回显**仅
   管理员可见**的调试信息获得）填入 `admins`；
4. 灰度：先只在私聊验证（`groups` 留空），再按需加群白名单；
5. `enable_save`/`auto_detect_links` 保持默认关闭，直到权限定义明确并
   真机验证。

### 回退步骤

- 应急停用：AstrBot 面板禁用插件（所有 `/pb` 与被动识别立即失效）；
- 收紧权限：清空 `users`/`groups`（等价于全拒绝，不需要重部署后端）；
- 代码回滚：插件改动独立成 commit（见 git log），`git revert` 即可；
- 后端无任何本特性相关改动，无需回滚。

## 11. 验收边界

**已由合成事件单测覆盖**（`tests/unit/test_plugin_authz.py`）：
允许/拒绝、跨平台实例同名编号、未授权群、机器人实例过滤、管理员能力、
save 开关、任务归属、扫码会话归属、被动识别放行条件、插件共存。

**必须真实 QQ 环境验收**（本仓库无法覆盖）：

- QQ 官方平台事件里 `platform_id`/`bot_id`/OpenID/群 ID 的实际字段值与
  格式（四元组提取依赖 `platform_id` 等属性，需真机核对）；
- 群聊 @ 触发、私聊被动回复窗口、`event.send` 主动推送成功率；
- 二维码图片经官方平台发送的可用性；
- 与作业插件在同一实例上的实际共存（消息不互吞）；
- 被动识别开启后的误触发率（建议保持关闭）。
