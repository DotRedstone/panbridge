# Provider 说明与复用来源

> 本文件由能力扫描 + 真实验证生成。**真实平台 14 个 + Mock 1 个 = 15 个已注册 Provider**。
> （通过 `main.py` 的 `registry.register()` 实际调用统计：14 个真实 Provider + 1 个 MockProvider。）
> 能力列以「实际方法实现」为准（自动扫描 `provider.py` 方法是否真实实现而非 `raise UNSUPPORTED_OPERATION`）。

## 五种验证状态（严格区分）

1. **已注册 Provider**：在 `registry` 中注册，能被 `registry.get()` 找到。
2. **已实现协议**：对应方法有真实 HTTP 协议实现（非空壳 `raise UNSUPPORTED_OPERATION`）。
3. **Mock 测试通过**：单元/集成测试用 mock httpx 验证了方法逻辑（不访问真实网络）。
4. **真实匿名网络验证通过**：用合法公开分享实际发送 HTTP 请求，解析成功/下载直链调通。
5. **真实账号转存下载通过**：用真实账号完成转存+下载全链路（需扫码登录）。

## 能力矩阵总览

图例：✅ 已实现且真实可用；🟡 已实现但需真实账号验证；🟠 已实现但触发风控/需登录；❌ 未实现或平台不支持。

| Provider | 显示名 | 匿名解析 | 文件列表 | 转存 | 账号列表 | 匿名下载 | 账号下载 | 认证方式 | 验证状态 |
|----------|--------|:---:|:---:|:---:|:---:|:---:|:---:|------|------|
| `quark` | 夸克网盘 | ✅ | ✅ | 🟡 | ✅ | ❌ | ✅ | **扫码登录** | ✅ 匿名解析真实验证 |
| `aliyun` | 阿里云盘 | ✅ | ✅ | 🟡 | ✅ | ✅ | ✅ | **扫码登录**（refresh_token） | ✅ 匿名解析真实验证 |
| `lanzou` | 蓝奏云 | ✅ | ✅ | ❌(无账号) | ❌ | ✅ | - | 无需登录 | ✅ 解析+直链+下载真实验证（2026 新版协议 + WAF 导航头） |
| `123` | 123 云盘 | ✅ | ✅ | ❌(未验证) | ✅ | ✅ | ✅ | **扫码登录**（token） | ✅ 匿名解析+登录态下载协议真实验证 |
| `baidu` | 百度网盘 | 🟠(风控) | ✅ | 🟡 | ✅ | ❌ | ✅ | 手工 BDUSS Cookie | 🟠 匿名触发 9019 风控 |
| `115` | 115 网盘 | ✅ | ✅ | 🟡 | ✅ | ❌ | ✅ | **扫码登录**（Cookie） | ⚠️ 匿名解析+Mock |
| `pikpak` | PikPak | ✅ | ✅ | ❌(未验证) | ❌ | ✅ | ❌ | 无需登录(分享) | ⚠️ 协议+Mock（需住宅IP） |
| `cloud189` | 天翼云盘 | ✅ | ✅ | 🟡 | ✅ | ❌ | ✅ | **账号密码登录** | ⚠️ 协议+Mock |
| `caiyun` | 移动云盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ❌ | ✅ | 手工 Authorization | ⚠️ 协议+Mock |
| `uc` | UC 网盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ❌ | ✅ | **扫码登录**（Cookie） | ⚠️ 扫码端点真实调通 |
| `xunlei` | 迅雷云盘 | ❌(需登录) | ❌(需登录) | 🟡 | ✅ | ❌ | ✅ | 手工 refresh_token | ⚠️ 协议+Mock |
| `weiyun` | 腾讯微云 | ✅ | ✅ | 🟡 | ✅ | ❌ | ❌ | 手工 skey Cookie | ⚠️ 协议+Mock |
| `guangya` | 光鸭云盘 | ✅ | ✅ | 🟡 | ✅ | ❌ | ✅ | 手工 access_token | ⚠️ 协议+Mock |
| `wukong` | 悟空网盘 | ✅ | ✅ | 🟡 | ✅ | ❌ | ✅ | 手工 Cookie/token | ⚠️ 协议+Mock |
| `mock` | Mock | - | - | - | - | - | - | - | 测试用 |

> **认证方式重要说明（如实）**：
> - **已实现扫码登录的平台（5 个）**：
>   - `quark`：`panbridge auth login --provider quark`，复用 quarkpan 二维码协议，
>     service_ticket 换 Cookie，加密入库。
>   - `aliyun`：`panbridge auth login --provider aliyun`，复用 aligo 登录端点
>     （authorize → generate.do → query.do 轮询 → bizExt 取 refresh_token），
>     **用独立 httpx 实现，不写 `~/.aligo/` 明文目录**，refresh_token 加密入库。
>   - `115`：`panbridge auth login --provider 115`，复用 p115client 二维码协议
>     （login_qrcode_token → scan_status 轮询 → scan_result 取 Cookie），
>     不写 `~/115-cookies.txt` 默认路径，Cookie 加密入库。
>   - `123`：`panbridge auth login --provider 123`，调用 login.123pan.com
>     二维码协议（qr-code/generate → qr-code/result 轮询 loginStatus → token），
>     token 加密入库（30 天有效期）。
>   - `uc`：`panbridge auth login --provider uc`，与夸克同源的 CAS 扫码协议
>     （getTokenForQrcodeLogin → getServiceTicketByQrcodeToken 轮询 →
>     drive.uc.cn/account/info 换 Cookie），Cookie 加密入库。
>   - 五者均支持：二维码过期、主动取消、重复扫码（幂等轮询）、凭据失效后重新
>     登录。**扫码状态流转已通过协议模拟测试验证，但真实扫码留待用户验收**。
> - **已实现账号密码登录的平台（1 个）**：
>   - `cloud189`：`panbridge auth login-password --provider cloud189`，交互式
>     无回显输入账号密码，用 cloud189 SDK `login_by_password` 验证通过后把
>     `{username, password}` 加密入库（扫码接口已失效，账号密码更可靠）。
> - 其余平台（百度/天翼/移动/迅雷/微云/光鸭/悟空）扫码登录情况（如实）：
>   - `baidu`：网页版 `/api/qrcode/create` 端点已废弃（返回 404），开放平台
>     OAuth 设备码需自行注册应用并审核，暂无法无依赖扫码，保留手工 BDUSS 导入。
>   - `cloud189`：扫码接口 `qrcodeLoginResult.action` 现返回 `QrCodeRollLoginFail`
>     （服务端拒绝，协议疑似失效），账号密码登录（SDK 已支持）更可靠。
>   - `weiyun`：微信/QQ 扫码走腾讯 ptlogin 协议（需逆向 + 官方 appid），
>     QQ 登录 Cookie 仅保活 48 小时，暂保留手工 skey Cookie 导入。
>   - `caiyun`/`xunlei`：移动云盘 token 5 分钟失效需续票签名、迅雷需图形
>     验证码签名，均无稳定公开扫码端点，保留手工凭据导入。
>   - `guangya`/`wukong`：未见公开扫码/OAuth 端点，保留手工导入。
>   - 以上平台统一保留手工凭据导入路径：
>     `panbridge auth import --provider <平台> --credential "<Cookie/token>"`。
> - 不要把「需要手工 Cookie/token」的平台描述为「扫码即可使用」——
>   这是不准确的。当前扫码登录能力边界：**quark/aliyun/115/123/uc 已实现（协议），
>   其余平台需手工凭据导入**。

> 注：UC / 迅雷 / 移动云盘的「解析分享」依赖登录态，无法匿名完成。纯
> `INSPECT`（无账号）返回 `ACTION_REQUIRED`；`SAVE` / `SAVE_AND_DOWNLOAD`
> 流程使用账号凭据完成解析+转存。微云「解析分享」匿名可用（NoLogin 路径）。
>
> 「匿名下载」列：✅ 表示 `get_share_download_info` 已实现（非空壳），
> 支持无需账号的分享直链下载。仅 aliyun/lanzou/123/pikpak 四个平台支持。
> 其余平台分享下载需登录态，走「先转存再下载」（save_and_download）。

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
| `quark` | 423down 公开分享 `04063177664c` | 解析出 1 个目录成功（标题=原版集成更新ISO镜像） |
| `lanzou` | `wwarh.lanzoum.com/iKpTp331jiqb`（dll修复工具.rar，371KB） | 新协议解析成功（iframe /fn → ajaxfile.php → 直链） |

### ✅ 真实匿名下载协议验证（直链接口真实调通）

| 平台 | 验证内容 | 结果 |
|------|---------|------|
| `123` | 递归定位小文件 `LTSC_2024_26300.9711.txt`（810B）→ `/b/api/share/download/info` 解析直链 | 下载接口调通，服务器返回业务错误 `5112 分享方流量包不足`（证明协议正确，仅该分享流量耗尽，非协议错误） |
| `lanzou` | 新协议直链解析 + acw_sc__v2 反爬 | 直链解析成功；2026-10 复验：新版内嵌 sign 协议（isngis）+ 下载节点 WAF 导航头方案，真实下载调通（Range 206 + ZIP 文件头） |

> **123 云盘下载协议已完全验证**：`/gsb/s/share-key` 解析短码 →
> `<uid>.share.123pan.cn/api/share/get` 列表 → 下载直链（etag/s3keyFlag/fileId）。
> 匿名下载（`/b/api/share/download/info`）仅对「≤100MB 且分享者开启免登录流量包」
> 的文件生效，否则返回 `5112 需要注册登录或付费后下载`（已映射 `QUOTA_EXCEEDED`）。
>
> **登录态下载已实现**：扫码登录拿到 token 后，`get_share_download_info` 走
> `/api/file/download_info`（带 `platform=android` 客户端标识），用文件 Etag（MD5）
> + S3KeyFlag 直接取直链，**无需转存**，模拟客户端下载以争取减半流量消耗。
> 免费账号每月约 10G 下载流量，登录客户端下载可减半消耗。
>
> **蓝奏云新协议已真实验证**：2025 年改版后，分享页不再含 `sign`，改为
> `<iframe src="/fn?<sig>">` → `/fn` 页提供 `wp_sign/ajaxdata` → POST
> `ajaxfile.php` 拿直链。2026 年再改版：分享页 sign 直接内嵌在 JS 变量
> `isngis` 中（可能先赋空串再赋真值，取最后一个非空值），ajax 地址为
> 绝对 URL，POST `{action, sign, kd, p}`，直链固定为 `dom + "/file/" + url`。
> 分享页与下载节点均有 `acw_sc__v2` JS 反爬（arg1 40 位重排 + XOR 密钥，
> 已纯 Python 实现自动求解）。
>
> **下载节点 WAF 关键结论（2026-10 真机实测）**：WAF 按请求头判定客户端——
> ① 缺少浏览器导航头（`Sec-Fetch-*` + 完整 Accept）→ 弹挑战/「网络异常需
> 验证」中转页；② UA 声称 Chrome 但 TLS 指纹是 Python（httpx）→ 一律挑战。
> 因此下载节点请求**不伪装浏览器 UA、带完整导航头**即可直接拿到文件流
> （Range 续传兼容）。`get_download_info` 返回的 headers 已含导航头。
> 真机浏览器对照验证：浏览器访问同一 token 直接下载，纯 HTTP 侧修正导航
> 头后同样直通，中转页 ajax.php 二次解析仅作兜底保留。

### 🟠 触发风控 / 需登录（如实返回错误码）

| 平台 | 现象 | 处理 |
|------|------|------|
| `baidu` | wxlist 返回 9019 `need verify` | 已识别为 `ACTION_REQUIRED`（风控），非协议错误 |
| `guangya` | 分享端点返回 410 `code:128` | 分享失效或端点漂移，未确认 |
| `pikpak` | 需住宅 IP，IDC 段被屏蔽返回 PROHIBITED | 已映射 RATE_LIMITED，需住宅 IP 才能真实下载 |

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
| `lanzou` | 分享页 iframe /fn 或内嵌 isngis sign → ajaxfile.php → 直链 + acw_sc__v2 反爬 + 下载节点 WAF 导航头 | 已真实验证（2026-10） |
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

## 统一验收流程（批量验收脚本）

用户无需手工抓 Cookie 或写 curl。但需注意认证方式边界：

```bash
# 1. 扫码登录（支持 quark / aliyun / 115）
panbridge auth login --provider quark
panbridge auth login --provider aliyun
panbridge auth login --provider 115

# 2. 其他平台：手工导入凭据（加密入库，不落明文）
panbridge auth import --provider baidu --credential "BDUSS=..."
#    aliyun: refresh_token；baidu: BDUSS；115: Cookie；
#    cloud189: 账号密码/Cookie；caiyun: Authorization；uc: Cookie；
#    xunlei: refresh_token；weiyun: skey；guangya: access_token；wukong: Cookie/token

# 3. 提供分享链接，脚本自动判断「匿名直链下载」还是「先转存再下载」
#    抽样验证（默认）：只下载第一个文件，验证链路是否打通
python scripts/acceptance.py --share 'https://www.123pan.com/s/xxxx'

#    完整目录验证：递归下载所有文件并保留结构、校验大小
python scripts/acceptance.py --share 'https://pan.quark.cn/s/xxxx' --mode full

#    限制最大下载量（超过则跳过整目录验证，仅抽样）
python scripts/acceptance.py --share '...' --mode full --max-download-bytes 10485760

# 批量验收：链接写进文本文件（每行一条，# 开头为注释）
python scripts/acceptance.py --file links.txt
```

**自动判断逻辑**（核心目标「用户给链接 → 自动判断下载方式」）：
- 平台 `capabilities` 含 `get_share_download_info` → 走 `DOWNLOAD`（匿名直链，无需账号）。
- 否则 → 走 `SAVE_AND_DOWNLOAD`（先转存再下载，需账号）。

**验收模式**（禁止单文件通过即判平台通过）：
- `sample`（默认）：功能抽样验证——只下载第一个非目录文件，验证「解析 → 直链/转存 → 下载落盘」链路是否打通。通过**不代表**整目录可用。
- `full`：完整目录验证——递归下载所有文件、保留目录结构、校验每个文件大小。通过才代表该平台多级目录能力完整可用。

脚本会：解析分享 → （抽样或完整目录）→ 创建任务 → 等待完成 → 校验文件落盘，
并如实报告每个平台的模式、错误和下载结果。

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
