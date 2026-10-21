# PanBridge AstrBot 插件

通过 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 机器人调用
[PanBridge](../README.md) 多网盘转存/下载服务。在 QQ / 微信 / Telegram / 钉钉 /
飞书等任意 AstrBot 支持的平台上，发送网盘分享链接即可解析、转存、下载，并支持
扫码登录网盘账号。

## 功能

- **解析分享**：发送链接，自动识别平台、提取码，预览文件列表
- **转存 / 下载**：一键把分享文件转存到已登录账号或下载到本地
- **扫码登录**：在机器人对话里发起扫码，返回二维码图片，用对应 App 扫码登录
- **账号密码登录**：天翼云盘（cloud189）等平台走账号密码登录（见主项目 CLI）
- **任务管理**：查询任务进度、取消、重试

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

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `base_url` | PanBridge 服务地址 | `http://127.0.0.1:8000` |
| `api_key` | 服务端 `PANBRIDGE_API_KEY`（未配置鉴权时留空） | 空 |
| `default_provider` | 登录缺省平台 | `quark` |

> 若 AstrBot 与 PanBridge 不在同一台机器/容器，`base_url` 需填可访问的地址，
> 并注意 PanBridge 默认只监听 `127.0.0.1`（可用 `PANBRIDGE_BIND_HOST=0.0.0.0`
> 开放，配合 `PANBRIDGE_API_KEY` 鉴权）。

## 使用

| 指令 | 作用 |
|------|------|
| `/pb <链接>` | 解析并预览分享文件列表 |
| `/pb download <链接>` | 下载分享文件 |
| `/pb save <链接>` | 转存到已登录账号 |
| `/pb status [task_id]` | 查任务状态（缺省列最近任务） |
| `/pb login <provider>` | 扫码登录（quark/aliyun/115/123/uc） |
| `/pb accounts` | 列出已登录账号 |
| `/pb providers` | 列出支持平台 |
| `/pb help` | 帮助 |

也可以**直接发送网盘链接**（不带 `/pb` 前缀），插件会自动识别并解析。

### 示例

```
你: https://pan.quark.cn/s/abc123 提取码 1234
机器人: 平台：quark / 标题：xxx / 文件数：3 ...

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
| 百度 / 迅雷 / 微云 / 移动 / 光鸭 / 悟空 | 手工凭据 | `uv run panbridge auth import --provider <平台>` |

## 说明

- 插件只通过 HTTP 调用 PanBridge，不在插件本地存储任何网盘凭据；凭据由
  PanBridge 服务端加密存储。
- 下载/转存依赖已登录的对应平台账号，未登录时会提示先扫码。
- 免费账号受各平台流量/限速策略影响，详见 PanBridge 主项目文档。

## 开发

- 纯函数在 `utils.py`（不依赖 astrbot，可独立单测）。
- HTTP 客户端在 `panbridge_client.py`（可注入 `transport` 做 mock 测试）。
- 单元测试见主项目 `tests/unit/test_astrbot_plugin.py`。
