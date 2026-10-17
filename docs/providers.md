# Provider 说明与复用来源

## 能力矩阵总览

| Provider | 显示名 | 匿名解析分享 | 匿名直链 | 转存 | 账号文件列表 | 登录方式 | 验证状态 |
|----------|--------|:---:|:---:|:---:|:---:|------|------|
| `quark` | 夸克网盘 | ✅ | ❌ | ✅(需账号) | ✅ | 扫码登录 | ⚠️ 需账号验证 |
| `aliyun` | 阿里云盘 | ✅ | ✅ | ✅(需账号) | ✅ | refresh_token | ✅ 匿名已验/转存需账号 |
| `lanzou` | 蓝奏云 | ✅ | ✅ | ❌(无账号体系) | ❌ | 无需登录 | ✅ 协议实现+Mock |
| `123` | 123 云盘 | ✅ | ✅ | ⚠️(需账号) | ✅(需账号) | access_token | ✅ 匿名协议+Mock |
| `baidu` | 百度网盘 | ✅ | ⚠️(需Cookie) | ⚠️(需Cookie) | ⚠️(需Cookie) | BDUSS Cookie | ✅ 匿名列表+Mock |
| `115` | 115 网盘 | ✅ | ⚠️(需Cookie) | ⚠️(需Cookie) | ✅(需Cookie) | Cookie | ✅ 匿名解析+Mock |
| `pikpak` | PikPak | ✅ | ✅ | ⚠️(需账号) | ⚠️(需账号) | 无需登录(分享) | ✅ 协议+Mock |
| `cloud189` | 天翼云盘 | ❌ | ❌ | ❌ | ✅ | 账号密码 | ⚠️ SDK 文件列表+Mock |
| `mock` | Mock | - | - | - | - | - | 测试用 |

图例：✅ 已实现/可用；⚠️ 实现但需真实账号验证；❌ 平台不支持或未实现。

## 接入进度与验证状态（诚实声明）

### 已真实验证（无需凭据的网络测试）

| 平台 | 已验证能力 | 说明 |
|------|-----------|------|
| `aliyun` | 匿名分享解析（token/详情/文件列表） | 用真实公开分享验证，已取消分享正确映射 `INVALID_SHARE` |

### 已实现协议 + Mock 测试（未经真实账号验证）

| 平台 | 实现方式 | 核心协议 | 待人工验证 |
|------|---------|---------|-----------|
| `lanzou` | 自研 HTTP Adapter | 分享页提取 sign → POST ajaxm.php → 直链 | 真实分享链接解析/下载 |
| `123` | 自研 HTTP Adapter | CRC32 签名 → share/get 列表 → share/download/info 直链 | 真实分享解析/下载/转存 |
| `pikpak` | 自研 HTTP Adapter | MD5链 captcha_sign → share/detail 列表 → file_info 直链 | 真实分享解析/下载（需住宅 IP） |
| `baidu` | 自研 HTTP Adapter | wxlist 匿名列表 → tplconfig 签名 → sharedownload 直链(需Cookie) | 真实分享解析 + Cookie 直链 |

### 基于成熟 SDK（MIT）

| 平台 | SDK | 已实现能力 | 待人工验证 |
|------|-----|-----------|-----------|
| `quark` | quarkpan | 分享解析/转存/下载/扫码登录 | 转存/下载端到端 |
| `aliyun` | aligo | 分享解析/转存/下载 | 转存/refresh_token 登录 |
| `115` | p115client | 分享解析(匿名)/转存/下载/文件列表 | 转存/下载端到端 |
| `cloud189` | cloud189 | 账号文件列表/下载 | 分享解析(不支持) |

## 参考项目评估与许可证合规

| 项目 | 许可证 | 复用方式 | 结论 |
|------|--------|----------|------|
| [QuarkPan](https://github.com/lich0821/QuarkPan) | MIT | PyPI 依赖 `quarkpan` | ✅ 已采用 |
| [aligo](https://github.com/foyoux/aligo) | MIT | PyPI 依赖 `aligo` | ✅ 已采用 |
| [p115client](https://github.com/ChenyangGao/p115client) | MIT | PyPI 依赖 `p115client` | ✅ 已采用 |
| [cloud189](https://github.com/s0urcelab/cloud189) | MIT | PyPI 依赖 `cloud189` | ✅ 已采用 |
| [netdisk-fast-download](https://github.com/qaiu/netdisk-fast-download) | MIT | 仅参考协议 | ✅ 参考蓝奏云/123/UC 协议思路，代码独立编写 |
| [baidu-parse](https://github.com/xb18/baidu-parse) | MIT | 仅参考协议 | ✅ 参考百度 wxlist/sharedownload 协议，代码独立编写 |
| [alist](https://github.com/alist-org/alist) | AGPL-3.0 | **仅参考协议** | ⚠️ 不可复制代码（AGPL 传染），仅参考 123/PikPak 协议思路 |
| [pikdirect](https://github.com/gordonhwc/pikdirect) | MIT | 仅参考协议 | ✅ 参考 PikPak captcha_sign 协议 |
| [pikpakapi](https://github.com/Quan666/PikPakAPI) | GPL-3.0 | **不使用** | ❌ GPL 传染性，不可复制，仅参考协议思路 |
| [quark-auto-save](https://github.com/Cp0204/quark-auto-save) | AGPL-3.0 | 仅参考思路 | ❌ 不可复制代码 |
| [bypy](https://github.com/houtianze/bypy) | MIT | 未采用 | 百度 PCS 仅操作 `/apps/bypy`，不适合转存他人分享 |

> **许可证合规原则**：所有 Adapter 仅参考公开协议思路与 API 端点，代码均为
> 本项目独立编写；仅通过 PyPI 依赖引入 MIT 许可的 SDK，从不复制 AGPL/GPL
> 代码到本项目。

## 未接入平台（阻塞原因）

以下常见网盘当前**未实现**，原因如实记录：

| 平台 | 阻塞原因 |
|------|---------|
| UC 网盘 | 必须登录 Cookie 才能解析（netdisk-fast-download 确认），协议端点未公开、无成熟 SDK，且需短信验证码等特殊认证，无法匿名或离线验证 |
| 迅雷云盘 | 需账号登录（密码/短信），`api-pan.xunlei.com` 端点未公开、协议不稳定，无成熟 Python SDK |
| 腾讯微云 | 仅有 2015 年老协议（weiyun-api，MIT）与需 Token 的 MCP 实现，分享解析/转存协议不公开 |
| 中国移动云盘（和彩云） | 需 Authorization（Basic token），分享页走独立 `caiyun.139.com` 通道，协议未公开、需短信验证码 |

这些平台遵循「不伪造支持、不绕过验证码/风控」的原则，暂不实现空方法骨架。

## 新增 Provider 的规范

每新增一个 Provider 必须：

1. 实现统一能力声明（`capabilities`）
2. 错误映射为统一 `DomainError` 错误码
3. 提供可离线运行的 Mock 测试
4. 在 `docs/providers.md` 记录依赖来源、版本、复用方式、许可证和已验证能力
5. 平台协议变化时只改对应 Adapter，不改外部 API
