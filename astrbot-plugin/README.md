# PanBridge AstrBot 插件

通过 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 机器人调用
[PanBridge](../README.md) 多网盘转存/下载服务。在 QQ / 微信 / Telegram / 钉钉 /
飞书等任意 AstrBot 支持的平台上，发送网盘分享链接即可解析、转存、下载，并支持
扫码登录网盘账号。

## 功能

- **直接发链接即解析**：授权用户直接发送网盘分享链接（或含提取码的完整
  文案），机器人自动识别平台、解析并返回**简洁文件预览**——无需 `/pb`
  前缀、无需选择平台。**只预览，不自动下载/转存。**
- **转存 / 下载**：`/pb download` 下载、`/pb save` 转存（默认关闭），作为
  高级操作入口
- **扫码登录**：在机器人对话里发起扫码，返回二维码图片，用对应 App 扫码登录（仅管理员）
- **账号密码登录**：天翼云盘（cloud189）等平台走账号密码登录（见主项目 CLI）
- **任务管理**：查询/取消/重试**自己创建的**任务（管理员可查看全局最近任务）

### 自动预览与追问

- 链接完整、无需提取码、平台支持匿名解析 → 直接返回预览；
- **提取码缺失/错误**（`INVALID_PASSWORD`）→ 追问并提示补提取码的写法；
- **平台需要登录态**（`ACTION_REQUIRED`，如 UC/迅雷/移动云盘）→ 说明需
  管理员先登录，不编造结果；
- **链接不完整/无法识别**（`INVALID_SHARE`）→ 追问完整链接；
- 其余错误（限流、连接失败等）→ 简洁失败提示，不泄露上游细节。

## 权限边界（重要）

本插件**默认拒绝**所有用户，只有显式配置在白名单里的身份才能使用。
权限由统一机器人业务主线（如作业插件所在主线）确定，本插件只落实配置里的
授权结果，不维护自己的用户身份库：

- 身份 = 平台实例 + 机器人实例 + 用户 OpenID + 群（四元组互相区分）；
  不信任昵称、消息正文中的自称或 QQ 号文本
- 登录 / 账号列表等**凭据相关操作仅管理员**可用
- 转存（`/pb save`）默认全局关闭（`enable_save: false`），权限定义明确前
  不要打开
- 任务只能由创建者查询/取消/重试；插件重启后登记丢失，存量任务仅管理员可操作
- **直接发送网盘链接的被动识别默认开启**（`auto_detect_links: true`）：
  授权 + 会话范围检查通过时自动预览，**只预览不下载/转存**，解析成功会
  消费该事件（防止其他插件重复响应同一链接）；未授权/未命中链接的消息
  一律静默放行，不影响作业插件或其他插件

接入契约（命令与能力清单、可信身份字段、HTTP 认证、QQ 平台限制、部署与
回退步骤）见 [`../docs/bot-integration.md`](../docs/bot-integration.md)。

## 前置条件

1. 一个可访问的 **PanBridge 服务**（`uv run panbridge` 或 `uv run uvicorn
   panbridge.main:app`）。插件通过 HTTP 调用它。
2. 一个运行中的 **AstrBot**（Docker 或 `uv run main.py` 均可）。

## 安装

把本目录复制到 AstrBot 的插件目录（目录名需与 `metadata.yaml` 的 `name` 一致）：

```bash
git clone <本仓库> AstrBot/data/plugins/astrbot_plugin_panbridge
```

或直接在 AstrBot 的 `data/plugins/` 下手动创建目录并放入
`main.py`、`utils.py`、`panbridge_client.py`、`metadata.yaml`、`requirements.txt`。

然后在 AstrBot 管理面板的「扩展 → 插件」中启用本插件，并配置：
配置表单由同目录的 `_conf_schema.json` 提供；安装时须一并复制。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `base_url` | PanBridge 服务地址 | `http://127.0.0.1:8000` |
| `api_key` | 服务端 `PANBRIDGE_API_KEY`（服务间通道鉴权，**不是**用户身份） | 空 |
| `default_provider` | 登录缺省平台 | `quark` |
| `admins` | 管理员身份键列表（`平台实例:OpenID`），如 `["course-qq-official:OPENID_ABC"]` | 空（全拒绝） |
| `users` | 普通朋友身份键列表 | 空 |
| `groups` | 允许使用网盘功能的群列表（`平台实例:群ID`） | 空（仅私聊） |
| `allowed_bots` | 允许接入的机器人实例 ID | 空（不限制） |
| `enable_save` | 是否开放 `/pb save` 转存 | `false` |
| `auto_detect_links` | 直接发链接的被动识别（仅预览，不下载/转存） | `true` |

> `admins` 与 `users` 都为空时视为「未配置授权」，**所有人一律拒绝**。
>
> 若 AstrBot 与 PanBridge 不在同一台机器/容器，`base_url` 需填可访问的地址，
> 并注意 PanBridge 默认只监听 `127.0.0.1`（可用 `PANBRIDGE_BIND_HOST=0.0.0.0`
> 开放，配合 `PANBRIDGE_API_KEY` 鉴权）。

## 使用

| 指令 | 作用 | 权限 |
|------|------|------|
| `直接发链接` | 自动识别、解析并返回简洁预览（只预览，不下载/转存） | 白名单用户 |
| `/pb <链接>` | 同「直接发链接」，显式解析预览 | 白名单用户 |
| `/pb download <链接>` | 下载分享文件 | 白名单用户 |
| `/pb save <链接>` | 转存到已登录账号 | 默认关闭（`enable_save`） |
| `/pb status [task_id]` | 查任务状态（缺省列自己的任务；管理员列全局最近） | 任务归属校验 |
| `/pb cancel <task_id>` | 取消自己创建的任务 | 任务归属校验 |
| `/pb retry <task_id>` | 重试自己创建的任务 | 任务归属校验 |
| `/pb login <provider>` | 扫码登录（quark/aliyun/115/123/uc） | 仅管理员 |
| `/pb accounts` | 列出已登录账号 | 仅管理员 |
| `/pb providers` | 列出支持平台 | 白名单用户 |
| `/pb help` | 帮助 | 白名单用户 |

未授权用户使用 `/pb` 只会得到统一的「未开放」提示；未授权用户直接发链接
**无任何响应**（静默放行，不吞其他插件的消息）。

### 示例

```
你: https://pan.quark.cn/s/abc123 提取码 1234
机器人: 平台：quark / 标题：xxx / - a.mkv (1.0MB) ...
        如需下载请发送 /pb download <链接>。

你: /pb download https://pan.quark.cn/s/abc123
机器人: 已创建任务 xxx（download），正在排队处理。发送 /pb status xxx 查询进度。

你: /pb login quark
机器人: 请用夸克网盘 App 扫码登录（二维码见下）[二维码图片]
      ✅ quark 登录成功（账号 xxx）。
```

## 账号登录方式速查

| 平台 | 登录方式 | 命令 |
|------|---------|------|
| 夸克 / 阿里 / 115 / 123 / UC | 扫码 | `/pb login <provider>` |
| 天翼云盘 | 账号密码 | 在 PanBridge 主机上 `uv run panbridge auth login-password` |
| 百度 / 迅雷 / 微云 / 移动 | 手工凭据 | `uv run panbridge auth import --provider <平台>` |

## 说明

- 插件只通过 HTTP 调用 PanBridge，不在插件本地存储任何网盘凭据；凭据由
  PanBridge 服务端加密存储。
- 下载/转存依赖已登录的对应平台账号，未登录时会提示先扫码。
- 免费账号受各平台流量/限速策略影响，详见 PanBridge 主项目文档。

## 开发

- 纯函数在 `utils.py`（不依赖 astrbot，可独立单测）：链接识别
  `find_pan_link`、解析错误分类 `classify_inspect_error`、简洁预览
  `format_inspect`、追问文案 `inspect_followup_text`。
- HTTP 客户端在 `panbridge_client.py`（可注入 `transport` 做 mock 测试）。
- 单元测试见主项目 `tests/unit/test_astrbot_plugin.py`（客户端与纯函数）
  和 `tests/unit/test_plugin_authz.py`（授权边界、被动识别、插件共存）。
- 事件消费（`stop_event`）与真实 QQ 群聊 @、私聊回执等需真机验收，见
  [`../docs/bot-integration.md`](../docs/bot-integration.md)「验收边界」。
