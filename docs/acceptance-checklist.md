# PanBridge 最终验收清单

> 本清单用于最终真人验收。区分「自动化测试已证明」「真实匿名网络已验证」「仍需本人扫码确认」三类，避免把模拟测试成功误当作真实扫码成功。

## 一、验收命令（与当前 argparse 完全一致）

### 1. 扫码登录（三套）

```bash
# 夸克网盘扫码登录
panbridge auth login --provider quark

# 阿里云盘扫码登录
panbridge auth login --provider aliyun

# 115 网盘扫码登录
panbridge auth login --provider 115
```

> 每条命令会在终端渲染 ASCII 二维码，**并同时落盘一张 PNG 图片**
> （`/tmp/panbridge-<provider>-login.png`），用图片查看器打开后用手机 App 扫，
> 比终端黑块更清晰。渲染失败时兜底打印可手动打开的链接。扫码后自动完成
> 凭据加密入库，最多等待 5 分钟。

### 2. 真实账号转存 + 下载（最简验收）

```bash
# 先完成对应平台的扫码登录（见上），再运行：
python scripts/acceptance.py --share 'https://pan.quark.cn/s/xxxx 提取码: yyyy'
```

- 匿名可下载的平台（123 云盘、蓝奏云等）无需账号，直接跑。
- 需账号的平台（夸克等）会走「转存 → 下载」链路。
- 默认 `--mode sample` 只下载第一个非目录文件；`--mode full` 递归下载整目录。

### 3. 手工凭据导入（未实现扫码的平台，如百度/UC/迅雷/微云）

```bash
# 安全方式：不带 --credential，交互式无回显输入（推荐）
panbridge auth import --provider baidu --label work

# CI/脚本：从环境变量读取（不经过命令行参数）
PANBRIDGE_IMPORT_CREDENTIAL='BDUSS=...' panbridge auth import --provider baidu

# 从 stdin 管道读取
printf '%s' 'BDUSS=...' | panbridge auth import --provider baidu
```

> `--credential` 参数仅为兼容保留，会打印泄漏风险警告，不建议使用。

### 4. 免扫码登录：从浏览器复制 Cookie 导入（夸克等平台）

如果不想扫码，也可以直接从浏览器复制登录 Cookie 一次性导入，之后长期免扫码：

1. 电脑浏览器登录 `https://pan.quark.cn/`（夸克网页版）。
2. 按 `F12` 打开开发者工具 → **Network（网络）** 标签 → 刷新页面。
3. 点任意一个 `pan.quark.cn` 的请求 → **Headers** → 找到 **Request Headers**
   里的 `Cookie:`，复制整行值（`name=value; name2=value2; ...` 的完整字符串）。
4. 导入（交互式无回显，Cookie 不落 Shell 历史）：

   ```bash
   panbridge auth import --provider quark --label my-quark
   # 提示后粘贴 Cookie 并回车（输入不回显）
   ```

   > 也可以从环境变量或 stdin 安全导入，见上一节。

导入后即可用 `panbridge auth status --provider quark` 查看登录状态，或直接
`python scripts/acceptance.py --share '...'` 走转存下载，无需扫码。

---

## 二、验收清单

### A. 已由自动化测试证明（无需真人操作）

| 项 | 说明 | 状态 |
| --- | --- | --- |
| 安全逐跳重定向 | CDN 跳转跟随、每跳 SSRF/DNS 校验、跨域剥离敏感头、跳数限制 | ✅ 14 个本地 HTTP 真实字节测试通过 |
| 断点续传累计限额 | 续传时 `max_size` 按累计字节判定，超限中止并清理 `.part` | ✅ 自动化测试通过 |
| 重定向防 SSRF | 跳转到内网/元数据地址被拦截 | ✅ 自动化测试通过 |
| 扫码会话编排 | 过期/取消/重复扫码/凭据加密入库 | ✅ `test_auth.py` 通过 |
| 三套扫码协议模拟 | quark/aliyun/115 状态流转（waiting/scanned/success/expired/cancelled） | ✅ `test_qr_backends.py` 通过 |
| 凭据加密存储 | Fernet 加密，密文不含明文 | ✅ `test_cli_import.py` 通过 |
| 凭据输入安全 | 不进命令行参数（getpass/env/stdin 三路径） | ✅ 自动化测试通过 |
| API → Worker 端到端 | HTTP 创建任务 → Worker 执行 → 文件落盘 → 查询 | ✅ 38 个集成测试通过 |
| 转存 fid 映射严格性 | 不伪造源 fid、扁平转存含子目录时显式报错 | ✅ 自动化测试通过 |
| 全量测试 + Ruff | 259+ passed，Ruff 零告警 | ✅ |

### B. 已由真实匿名网络验证（无需账号，但仍建议复核）

| 项 | 说明 | 状态 |
| --- | --- | --- |
| 123 云盘匿名解析 + 直链下载 | `scripts/e2e_anon_download.py` 用公开分享验证真实字节 | ⚠️ 需联网运行脚本确认 |

> 匿名验证脚本：
> ```bash
> PYTHONPATH=src python scripts/e2e_anon_download.py
> ```

### C. 必须由本人扫码确认（无法自动化）

| 项 | 命令 | 说明 |
| --- | --- | --- |
| 夸克扫码登录 | `panbridge auth login --provider quark` | 需真实夸克 App 扫码，验证 `service_ticket → Cookie` 换取 |
| 阿里云盘扫码登录 | `panbridge auth login --provider aliyun` | 需真实阿里云盘 App 扫码，验证 `refreshToken` 提取 |
| 115 扫码登录 | `panbridge auth login --provider 115` | 需真实 115 App 扫码，验证 cookie 提取 |
| 真实账号转存下载 | `python scripts/acceptance.py --share '...'` | 需登录后提供真实分享，验证「解析 → 转存 → 下载」全链路 |

> **重要**：三套扫码的「协议模拟测试通过」只证明状态机与字段映射正确，
> **不代表真实扫码成功**。真实扫码仍须本人用对应 App 操作确认。

---

## 三、本轮修复摘要

1. **HttpDownloader 重定向**：从「简单禁用重定向（会导致真实 CDN 跳转下载失败）」
   改为「安全逐跳跟随」——每跳重新做 URL/DNS/IP/SSRF 校验、跨域剥离
   Cookie/Authorization 等敏感头、限制最大跳数（默认 5）。
2. **`auth import` 安全**：凭据不再必须走 `--credential` 命令行参数，
   改为交互式无回显（getpass）/环境变量/stdin 三路径，加密后存储。
3. **扫码 SDK 签名核对**：quark/aliyun/115 三套后端与真实 SDK 方法签名、
   状态枚举、凭据提取逻辑逐一比对，确认一致，无新增缺陷。
