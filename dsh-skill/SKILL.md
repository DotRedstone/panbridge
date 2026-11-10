---
name: panbridge
description: 通过 PanBridge 后端服务解析/转存/下载网盘分享链接（夸克、阿里云盘、百度、115、123、蓝奏云、天翼云盘、移动云盘、UC、迅雷、微云、PikPak 等）。当用户要求「解析这个网盘链接」「转存到我的网盘」「下载这个分享」「查一下转存/下载进度」「扫码登录网盘」时使用。
---

# PanBridge 网盘转存/下载（panbridge）

把用户的网盘分享链接交给本地 PanBridge HTTP 服务处理：解析文件列表 → 转存到已登录账号 → 下载到本地 → 查询任务进度。脚本是零依赖 CLI，只调 PanBridge 的 `/v1` API。

## 何时用

- 「解析下这个夸克链接」「这个分享里有什么」→ `inspect`
- 「帮我转存到我的网盘」→ `save`
- 「下载这个分享」→ `download`
- 「转存并下载」→ `save_and_download`
- 「进度怎么样了」「任务成功了吗」→ `status` / `tasks`
- 「扫码登录网盘」→ `login`（仅管理员场景）

## 前置：PanBridge 服务需在运行

脚本默认连 `http://127.0.0.1:8000`。若服务没起，会返回 `CONNECTION` 错误，需先启动：

```bash
cd ~/Projects/panbridge
# 进 devShell 后（nix develop），用 .venv 直接启动
.venv/bin/python -m uvicorn panbridge.main:app --host 127.0.0.1 --port 8000
```

鉴权（`X-API-Key`）三选一，优先级从高到低：

1. `--api-key "..."`（命令行）
2. 环境变量 `PANBRIDGE_API_KEY`
3. 文件 `~/.dsh/panbridge/api_key`（建议 `chmod 600`）

服务端未配置 `PANBRIDGE_API_KEY`（开发模式）时留空即可，无需任何 key。

## 执行

配套脚本：`scripts/panbridge.py`（纯标准库，零第三方依赖）。

```bash
# 基础配置
--base-url http://127.0.0.1:8000     # 默认；也可用 PANBRIDGE_BASE_URL
--api-key <key>                       # 默认读 PANBRIDGE_API_KEY / ~/.dsh/panbridge/api_key

# 1) 解析分享，预览文件列表（含目录结构）
python3 scripts/panbridge.py inspect 'https://pan.quark.cn/s/xxxx 提取码: abcd'

# 2) 列出支持平台
python3 scripts/panbridge.py providers

# 3) 转存到已登录账号（需先 login 或已有账号）
python3 scripts/panbridge.py save 'https://pan.quark.cn/s/xxxx 提取码: abcd'
python3 scripts/panbridge.py save '链接' --destination 我的转存 --on-conflict skip

# 4) 直接下载（平台支持匿名直链时无需账号）
python3 scripts/panbridge.py download 'https://www.123pan.com/s/xxxx'

# 5) 先转存再下载（平台不支持匿名下载时）
python3 scripts/panbridge.py save_and_download 'https://pan.quark.cn/s/xxxx'

# 6) 只处理部分文件
python3 scripts/panbridge.py download '链接' --selection file_id1 file_id2

# 7) 查任务 / 任务列表 / 产出文件
python3 scripts/panbridge.py status <task_id>
python3 scripts/panbridge.py tasks --limit 10
python3 scripts/panbridge.py files <task_id>

# 8) 取消 / 重试
python3 scripts/panbridge.py cancel <task_id>
python3 scripts/panbridge.py retry <task_id>

# 9) 账号与扫码登录
python3 scripts/panbridge.py accounts
python3 scripts/panbridge.py login quark          # 返回二维码/登录入口
python3 scripts/panbridge.py qr-status <session_id>
python3 scripts/panbridge.py login-status quark
```

所有命令输出 JSON（stdout）；出错时 JSON 打到 stderr 并返回非零退出码。

## 任务状态机

`status` 返回的 `status` 字段：

- `queued` → `resolving` → `saving` / `downloading` → `completed`
- 失败：`failed`（含 `error_code` / `error_message`）；可重试的用 `retry`
- 取消：`cancelled`

`save`/`download` 是异步的：`create` 后拿到 `id`，用 `status <id>` 轮询直到终态（`completed`/`failed`/`cancelled`）。任务产出文件用 `files <id>` 列出，再经 PanBridge 的 `GET /v1/tasks/{id}/files/{filename}` 下载到本地。

## 支持的 action 语义

| action | 含义 | 是否需账号 |
| --- | --- | --- |
| `inspect` | 仅解析预览，不改任何东西 | 否 |
| `download` | 匿名直链下载（平台支持时） | 否（需账号的平台会自动报错） |
| `save` | 转存到已登录账号 | 是 |
| `save_and_download` | 先转存再下载 | 是 |

平台能力差异（谁能匿名下载/谁能转存）以 `providers` 返回的 `capabilities` 为准，不要假设所有平台一样。

## 注意

- 只做用户明确要求的操作；`inspect` 是纯预览，不下载不转存。
- 分享链接是不可信输入，交给后端解析；后端会识别平台、提取码并做路径穿越/SSRF 防护。
- 凭据只存在 PanBridge 后端（加密），脚本只传 `X-API-Key`，不接触任何 Cookie/Token。
- 服务默认监听 `127.0.0.1`，不要暴露公网端口。
