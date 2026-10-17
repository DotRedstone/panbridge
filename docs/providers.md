# Provider 说明与复用来源

> 本文件由能力扫描 + 真实验证生成。**真实平台 15 个 + Mock 1 个 = 16 个已注册 Provider**。
> 能力列以「实际方法实现」为准（自动扫描 `provider.py` 方法是否真实实现而非 `raise UNSUPPORTED_OPERATION`）。

## 能力矩阵总览

图例：✅ 已实现且真实可用；🟡 已实现但需真实账号验证；🟠 已实现但触发风控/需登录；❌ 未实现或平台不支持。

| Provider | 显示名 | 匿名解析 | 文件列表 | 转存 | 账号列表 | 下载 | 登录方式 | 验证状态 |
|----------|--------|:---:|:---:|:---:|:---:|:---:|------|------|
| `quark` | 夸克网盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | 扫码 | ✅ 匿名解析真实验证 |
| `aliyun` | 阿里云盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | refresh_token | ✅ 匿名解析真实验证 |
| `lanzou` | 蓝奏云 | ✅ | ✅ | ❌(无账号) | ❌ | ✅ | 无需登录 | ⚠️ 协议+Mock |
| `123` | 123 云盘 | ✅ | ✅ | ❌(未验证) | ✅ | 🟡 | access_token | ✅ 匿名解析真实验证(协议已修复) |
| `baidu` | 百度网盘 | 🟠(风控) | ✅ | 🟡 | ✅ | ✅ | BDUSS Cookie | 🟠 匿名触发 9019 风控 |
| `115` | 115 网盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | Cookie | ⚠️ 匿名解析+Mock |
| `pikpak` | PikPak | ✅ | ✅ | ❌(未验证) | ❌ | 🟡 | 无需登录(分享) | ⚠️ 协议+Mock |
| `cloud189` | 天翼云盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | 账号密码/SSON | ⚠️ 协议+Mock |
| `caiyun` | 移动云盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ✅ | Authorization | ⚠️ 协议+Mock |
| `uc` | UC 网盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ✅ | Cookie | ⚠️ 协议+Mock |
| `xunlei` | 迅雷云盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ✅ | refresh_token | ⚠️ 协议+Mock |
| `weiyun` | 腾讯微云 | ✅ | ✅ | 🟡 | ✅ | 🟡 | skey Cookie | ⚠️ 协议+Mock |
| `guangya` | 光鸭云盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | access_token | ⚠️ 协议+Mock |
| `wukong` | 悟空网盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | Cookie/token | ⚠️ 协议+Mock |
| `mock` | Mock | - | - | - | - | - | - | 测试用 |

> 注：UC / 迅雷 / 移动云盘的「解析分享」依赖登录态，无法匿名完成。纯
> `INSPECT`（无账号）返回 `ACTION_REQUIRED`；`SAVE` / `SAVE_AND_DOWNLOAD`
> 流程使用账号凭据完成解析+转存。微云「解析分享」匿名可用（NoLogin 路径）。

## 转存→下载闭环（save_and_download）

转存后需拿到账号网盘的**新文件 ID** 才能下载。各平台闭环情况：

| Provider | 转存返回新 fid | resolve 兜底 | 闭环状态 |
|----------|:---:|:---:|------|
| `aliyun` | ✅ | - | ✅ 完整 |
| `guangya` | ✅ | - | ✅ 完整 |
| `uc` | ✅ | - | ✅ 完整 |
| `weiyun` | ✅ | - | ✅ 完整 |
| `wukong` | ✅ | - | ✅ 完整 |
| `xunlei` | ✅ | - | ✅ 完整 |
| `quark` | ❌(异步) | ✅ | ✅ 完整 |
| `pan115` | ❌(异步) | ✅ | ✅ 完整 |
| `baidu` | ❌(task_id) | ✅ | ✅ 完整 |
| `caiyun` | ❌(task_id) | ✅ | ✅ 完整 |
| `cloud189` | ❌(task_id) | ✅ | ✅ 完整 |
| `lanzou` / `pikpak` / `123` | ❌(转存未实现) | - | ❌ 转存需账号验证 |

## 真实网络验证结果（2026-10-18）

### ✅ 真实匿名解析验证通过

| 平台 | 验证内容 | 结果 |
|------|---------|------|
| `123` | 423down 公开分享 `A6cA-I24Jh` | 解析出目录 + 目录遍历（子目录）成功 |
| `quark` | 423down 公开分享 `04063177664c` | 解析出 1 个目录成功 |
| `aliyun` | 此前已验证匿名分享解析 | 成功 |

### 🟠 触发风控 / 需登录（如实返回错误码）

| 平台 | 现象 | 处理 |
|------|------|------|
| `baidu` | wxlist 返回 9019 `need verify` | 已识别为 `ACTION_REQUIRED`（风控），非协议错误 |
| `guangya` | 分享端点返回 410 `code:128` | 分享失效或端点漂移，未确认 |

### ❌ 受阻平台（如实声明，不伪造支持）

| 平台 | 阻塞原因 |
|------|---------|
| 联通云盘 | 协议依赖 `wopan-sdk-go`（AGPL，需复现 AES 加密+签名）；分享解析无公开可靠协议；参考项目 WoOpen 无 LICENSE |
| 联通云盘(账号) | `dispatcher` 端点 + `Accesstoken` 头 + `clientId=1001000021`，body.param 为 SDK 加密 JSON，无法独立逆向 |

## 接入进度（诚实声明）

### 基于成熟 SDK（MIT）

| 平台 | SDK | 已实现 | 待人工验证 |
|------|-----|--------|-----------|
| `quark` | quarkpan | 分享解析/转存/下载/扫码登录 | 转存/下载端到端 |
| `aliyun` | aligo | 分享解析/转存/下载 | 转存/refresh_token 登录 |
| `115` | p115client | 分享解析(匿名)/转存/下载/列表 | 转存/下载端到端 |
| `cloud189` | cloud189 | 账号文件列表/下载 | 分享转存端到端 |

### 自研 HTTP Adapter（协议已实现 + Mock 测试，未真实账号验证）

| 平台 | 核心协议 | 待验证 |
|------|---------|--------|
| `lanzou` | 分享页 sign → ajaxm.php → 直链 | 真实分享解析/下载 |
| `123` | /gsb/s/share-key → /api/share/get → download/info | 转存（账号） |
| `pikpak` | MD5链 captcha_sign → share/detail → file_info | 需住宅 IP |
| `baidu` | wxlist → sharedownload(需Cookie) / 账号 /api/list+/api/download | Cookie 直链 |
| `caiyun` | AES-CBC+mcloud_sign → getOutLinkInfoV6 → IBatchOprTask | 转存/下载 |
| `uc` | sharepage/detail → save → file/download | 转存/下载 |
| `xunlei` | refresh_token+captcha → share → restore → files/{id} | 转存/下载 |
| `weiyun` | WeiyunShareView(NoLogin) → WeiyunSharePartSaveData | 转存 |
| `guangya` | /drive/v1/share → restore → files | 转存/下载 |
| `wukong` | /api/v1/share/info → save → file/download | 转存/下载 |
| `cloud189` | getShareInfoByCodeV2 → listShareDir → createBatchTask | 转存/下载 |

## 参考项目评估与许可证合规

| 项目 | 许可证 | 复用方式 | 结论 |
|------|--------|----------|------|
| [QuarkPan](https://github.com/lich0821/QuarkPan) | MIT | PyPI 依赖 | ✅ 已采用 |
| [aligo](https://github.com/foyoux/aligo) | MIT | PyPI 依赖 | ✅ 已采用 |
| [p115client](https://github.com/ChenyangGao/p115client) | MIT | PyPI 依赖 | ✅ 已采用 |
| [cloud189](https://github.com/s0urcelab/cloud189) | MIT | PyPI 依赖 | ✅ 已采用 |
| [pan-relay](https://github.com/ucmao/pan-relay) | MIT | 仅参考协议 | ✅ 参考 UC/迅雷/移动/光鸭/悟空协议 |
| [netdisk-fast-download](https://github.com/qaiu/netdisk-fast-download) | MIT | 仅参考协议 | ✅ 参考蓝奏云/123/UC 协议 |
| [cloud189-auto-save](https://github.com/josbu/cloud189-auto-save) | MIT | 仅参考协议 | ✅ 参考天翼云盘分享/转存协议 |
| [baidu-parse](https://github.com/xb18/baidu-parse) | MIT | 仅参考协议 | ✅ 参考百度 wxlist/sharedownload |
| [bddisk-shell](https://github.com/Cra1gTucker/bddisk-shell) | MIT | 仅参考协议 | ✅ 参考百度 share/transfer 转存 |
| [pikdirect](https://github.com/gordonhwc/pikdirect) | MIT | 仅参考协议 | ✅ 参考 PikPak captcha_sign |
| [alist](https://github.com/alist-org/alist) | AGPL-3.0 | **仅参考协议** | ⚠️ 不可复制代码 |
| [OpenList](https://github.com/OpenListTeam/OpenList) | AGPL-3.0 | **仅参考协议** | ⚠️ 不可复制代码 |
| [wopan-sdk-go](https://github.com/OpenListTeam/wopan-sdk-go) | AGPL-3.0 | **不使用** | ❌ 联通云盘受阻原因 |
| [QuickSDK](https://gitee.com/ACANX/quick-sdk) | GPL-3.0 | **仅参考端点** | ⚠️ 微云分享 API 端点 |
| [pikpakapi](https://github.com/Quan666/PikPakAPI) | GPL-3.0 | **不使用** | ❌ GPL 传染性 |

> **许可证合规原则**：所有 Adapter 仅参考公开协议思路与 API 端点，代码均为
> 本项目独立编写；仅通过 PyPI 依赖引入 MIT 许可的 SDK，从不复制 AGPL/GPL
> 代码到本项目。
