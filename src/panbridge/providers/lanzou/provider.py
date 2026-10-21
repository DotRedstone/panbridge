"""蓝奏云 Adapter（匿名解析 + 直链下载）。

蓝奏云分享链接无需登录即可解析并下载，协议简单，自行实现轻量 HTTP
适配器（不依赖第三方 SDK）。协议参考多个公开的逆向文章与 MIT 开源
工具（netdisk-fast-download 等），但代码为本项目自行编写。

解析流程：
1. GET 分享页面，从 HTML 提取：
   - 文件名（`<title>` 或 `box` 区块）
   - `sign` 与 `action` 参数（`action=downprocess&sign=xxx&p=` 片段）
   - `ajaxm.php` 地址（页面内脚本 `url : '/ajaxm.php?file=...'`）
2. POST ajaxm.php，data={action, sign, p, ves}，Referer 为分享页。
3. 响应 JSON：`zt`(状态) / `dom`(下载域名) / `url`(路径) / `inf`(文件名)。
   真实直链 = `dom + '/file/' + url`。

支持域名：lanzou.com 系及其子域名（lanzoui / lanzoux / lanzouw /
lanzouy / lanzoub / lanzous 等），蓝奏云优享版 ilanzou.com 亦支持。

能力边界（如实声明）：
- 仅支持「解析分享 + 直链下载」，无账号体系，故不支持转存、
  账号文件列表、创建目录。
- 单文件 ≤100MB；文件夹分享在蓝奏云中表现为多个子页面，本 Adapter
  仅解析单文件页面（文件夹需逐页解析，暂不支持）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import (
    Account,
    Share,
    ShareFile,
    TransferResult,
)
from panbridge.providers.base import ShareProvider

logger = logging.getLogger(__name__)

# 蓝奏云分享域名集合（用于识别与校验）
_LANZOU_HOSTS = (
    "lanzou.com",
    "lanzous.com",
    "lanzoui.com",
    "lanzoux.com",
    "lanzouw.com",
    "lanzouy.com",
    "lanzoub.com",
    "lanzoup.com",
    "lanzouo.com",
    "lanzouq.com",
    "lanzoum.com",
    "ilanzou.com",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

# 从分享页提取 sign 参数：action=downprocess&sign=xxx&p=（旧协议）
_SIGN_RE = re.compile(r"action=downprocess&sign=([A-Za-z0-9_\-]+?)&p=")
# 页面脚本中的 ajax 接口：url : '/ajaxm.php?file=123'（旧协议）
_AJAX_RE = re.compile(r"url\s*:\s*'(/ajaxm\.php\?file=\d+)'")
# 新协议：分享页内嵌 iframe 指向 /fn?<signature>
_FN_RE = re.compile(r'iframe[^>]*src="(/fn\?[^"]+)"')
# 新协议：/fn 页面内的下载参数
_WP_SIGN_RE = re.compile(r"var\s+wp_sign\s*=\s*'([^']+)'")
_AJAXDATA_RE = re.compile(r"var\s+ajaxdata\s*=\s*'([^']*)'")
_KDNS_RE = re.compile(r"var\s+kdns\s*=\s*(\d+)")
_DOMAIN_RE = re.compile(
    r"var\s+domain1\s*=\s*'(https://[^']+/ajaxfile\.php\?file=\d+)'"
)
# 文件名（优先从标题/页面元素提取）
_TITLE_RE = re.compile(
    r"<title>\s*(.*?)\s*[-–|]?\s*蓝奏云.*?</title>", re.S | re.I
)
_FILENAME_RE = re.compile(
    r"class=\"n_box\".*?<b>(.*?)</b>", re.S
)
# 新协议文件名（div 或 title 中提取）
_NEW_FILENAME_RE = re.compile(
    r"<title>\s*(.*?)\s*-\s*蓝奏云", re.S
)
_NEW_FILENAME_DIV_RE = re.compile(
    r'font-size:\s*30px[^>]*>\s*([^<]+?)\s*</div>', re.S
)

# ---- acw_sc__v2 反爬（阿里云 WAF 系 JS 挑战）----
# 蓝奏云（及下载节点）在部分请求返回一段混淆 JS，需计算 acw_sc__v2
# cookie 后重试才能拿到真实内容。算法固定：
#   arg1（40 位 hex）按 _ACW_M 位置表重排，再与 _ACW_P 密钥逐字节 XOR。
_ACW_M = [
    0xF, 0x23, 0x1D, 0x18, 0x21, 0x10, 0x1, 0x26, 0xA, 0x9, 0x13, 0x1F,
    0x28, 0x1B, 0x16, 0x17, 0x19, 0xD, 0x6, 0xB, 0x27, 0x12, 0x14, 0x8,
    0xE, 0x15, 0x20, 0x1A, 0x2, 0x1E, 0x7, 0x4, 0x11, 0x5, 0x3, 0x1C,
    0x22, 0x25, 0xC, 0x24,
]
_ACW_P = "3000176000856006061501533003690027800375"
_ARG1_RE = re.compile(r"arg1='([0-9A-Fa-f]{40})'")


def _solve_acw_sc_v2(html: str) -> str | None:
    """从 JS 挑战页计算 acw_sc__v2 cookie 值；无挑战返回 None。"""
    m = _ARG1_RE.search(html)
    if not m:
        return None
    arg1 = m.group(1).upper()
    if len(arg1) != 40:
        return None
    q = [""] * 40
    for x in range(40):
        for z in range(40):
            if _ACW_M[z] == x + 1:
                q[z] = arg1[x]
    u = "".join(q)
    out = []
    for i in range(0, 40, 2):
        val = int(u[i : i + 2], 16) ^ int(_ACW_P[i : i + 2], 16)
        out.append(f"{val:02x}")
    return "".join(out)


class LanzouProvider(ShareProvider):
    name = "lanzou"
    display_name = "蓝奏云"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
            "get_share_download_info",
        }
    )

    def __init__(self) -> None:
        # 蓝奏云无需凭据，cipher 参数不适用
        pass

    # ---- 内部工具 ----

    @staticmethod
    def _share_url(share_id: str) -> str:
        """根据 share_id（形如 <host>/<key> 或纯 key）构造分享 URL。

        由于蓝奏云域名众多，`share_id` 实际保存的是完整 URL 路径，
        由 link_extraction 层传入。这里归一化处理。
        """
        if share_id.startswith("http"):
            return share_id
        # 纯 key 时使用默认域名（可能 404，调用方应传入完整 URL）
        return f"https://www.lanzou.com/{share_id}"

    @staticmethod
    def _ajax_url(share_url: str, page_ajax: str | None) -> str:
        """构造 ajaxm.php 的完整 URL。

        `page_ajax` 是页面脚本给出的相对路径（如 /ajaxm.php?file=123），
        其域名与分享页一致。若未提取到，回退到分享页同域名 + /ajaxm.php。
        """
        if page_ajax:
            base = share_url.split("/s/")[0].split("//")[-1]
            host = "https://" + base
            return host + page_ajax
        # 回退：分享页去掉 path 后接 /ajaxm.php
        from urllib.parse import urlparse

        parsed = urlparse(share_url)
        return f"{parsed.scheme}://{parsed.netloc}/ajaxm.php"

    @staticmethod
    def _map_error(status_code: int, data: dict[str, Any]) -> DomainError:
        zt = data.get("zt")
        info = str(data.get("info", "") or data.get("url", ""))
        if zt == 0:
            # zt=0 且 info 含密码相关提示 → 提取码错误
            if "密码" in info or "密码错误" in info or "提取" in info:
                return DomainError(
                    ErrorCode.INVALID_PASSWORD,
                    "lanzou share password incorrect",
                )
            if "失效" in info or "取消" in info or "不存在" in info:
                return DomainError(
                    ErrorCode.INVALID_SHARE,
                    "lanzou share does not exist or expired",
                )
        # 下载节点验证码/网络异常风控：需人工验证，非协议错误。
        if "验证" in info or "网络异常" in info:
            return DomainError(
                ErrorCode.ACTION_REQUIRED,
                "lanzou download requires captcha verification "
                "(risk control triggered)",
                retryable=False,
            )
        return DomainError(
            ErrorCode.INVALID_SHARE,
            f"lanzou parse failed: {info or status_code}",
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        url = self._share_url(share_id)
        info = await self._fetch_file_info(url, password)
        files = [ShareFile(file_id=info["direct_url"], name=info["name"])]
        return Share(
            provider=self.name,
            share_id=share_id,
            title=info["name"],
            files=files,
            password=password,
            raw=info,
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        share = await self.inspect_share(share_id, password)
        return share.files

    async def validate_account(self, account: Account) -> bool:
        # 蓝奏云无账号体系
        return True

    async def save_shared_files(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        destination: str | None,
        on_conflict: str,
    ) -> TransferResult:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "lanzou does not support saving shared files (no account system)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "lanzou does not support account file listing (no account system)",
        )

    async def get_share_download_info(
        self,
        share_id: str,
        password: str | None,
        file_id: str,
        file: ShareFile | None = None,
        account: Account | None = None,
    ) -> dict[str, Any]:
        """解析蓝奏云分享单文件直链（匿名）。

        share_id 为完整分享 URL，file_id 为 inspect 时得到的直链或文件 key。
        `file` 提供 name（inspect 已解析）；直链在本次调用内重新解析
        （蓝奏云直链有时效，需刷新），不长期缓存。
        """
        info = await self._fetch_file_info(share_id, password)
        return {
            "url": info["direct_url"],
            "name": info["name"] or (file.name if file is not None else None),
            "size": None,
            "checksum": None,
            "headers": {
                "User-Agent": _HEADERS["User-Agent"],
                "Referer": "https://www.lanzou.com/",
            },
        }

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        """返回直链下载信息。

        `file_id` 若已是直链（含 `/file/` 路径）直接返回；否则视为
        share key，重新解析。
        """
        is_direct = "/file/" in file_id
        if is_direct:
            return {
                "url": file_id,
                "name": "",
                "size": None,
                "checksum": None,
                "headers": {
                    "User-Agent": _HEADERS["User-Agent"],
                    "Referer": "https://www.lanzou.com/",
                },
            }
        info = await self._fetch_file_info(file_id, None)
        return {
            "url": info["direct_url"],
            "name": info["name"] or f"lanzou-{file_id}.bin",
            "size": None,
            "checksum": None,
            "headers": {
                "User-Agent": _HEADERS["User-Agent"],
                "Referer": "https://www.lanzou.com/",
            },
        }

    async def _fetch_file_info(
        self, share_id: str, password: str | None
    ) -> dict[str, Any]:
        """解析单文件分享页，返回 {direct_url, name}。

        支持两种协议：
        - 新协议（2025+）：分享页 iframe → /fn?<sig> → wp_sign/ajaxdata
          → POST ajaxfile.php 拿直链。
        - 旧协议：sign + ajaxm.php（fallback）。
        两者都可能先遇到 acw_sc__v2 JS 反爬，需先算 cookie。
        """
        url = self._share_url(share_id)
        async with httpx.AsyncClient(
            timeout=30.0, headers=_HEADERS, follow_redirects=True
        ) as client:
            html = await self._get_page(client, url)

            # 尝试新协议
            fn = _FN_RE.search(html)
            if fn:
                info = await self._parse_new_protocol(
                    client, url, fn.group(1), html, password
                )
                if info is not None:
                    return info

            # 旧协议 fallback
            sign = _extract_sign(html)
            page_ajax = _AJAX_RE.search(html)
            ajax_path = page_ajax.group(1) if page_ajax else None
            ajax_url = self._ajax_url(url, ajax_path)

            if not sign:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    "lanzou share sign not found (share may be expired)",
                )

            data = {
                "action": "downprocess",
                "sign": sign,
                "ves": 1,
            }
            if password:
                data["p"] = password

            resp2 = await client.post(
                ajax_url,
                data=data,
                headers={
                    "Referer": url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            try:
                j = resp2.json()
            except Exception:  # noqa: BLE001
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    "lanzou ajax response is not JSON",
                ) from None

            if j.get("zt") != 1:
                raise self._map_error(resp2.status_code, j)

            dom = j.get("dom", "")
            path = j.get("url", "")
            if not dom or not path:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    "lanzou returned empty download url",
                )
            direct_url = f"{dom}/file/{path}"
            name = j.get("inf") or _extract_filename(html) or path
            return {"direct_url": direct_url, "name": name}

    async def _get_page(
        self, client: httpx.AsyncClient, url: str
    ) -> str:
        """GET 页面，自动处理 acw_sc__v2 JS 反爬。"""
        resp = await client.get(url)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"lanzou share page returned {resp.status_code}",
            )
        html = resp.text
        acw = _solve_acw_sc_v2(html)
        if acw:
            client.cookies.set(
                "acw_sc__v2", acw, domain=_cookie_domain(url)
            )
            resp = await client.get(url)
            if resp.status_code != 200:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    f"lanzou share page returned {resp.status_code} "
                    "after anti-bot challenge",
                )
            html = resp.text
        return html

    async def _parse_new_protocol(
        self,
        client: httpx.AsyncClient,
        share_url: str,
        fn_path: str,
        share_html: str,
        password: str | None,
    ) -> dict[str, Any] | None:
        """解析新协议（iframe /fn → ajaxfile.php），失败返回 None 以回退旧协议。"""
        from urllib.parse import urlparse

        base = f"{urlparse(share_url).scheme}://{urlparse(share_url).netloc}"
        fn_html = await self._get_page(client, base + fn_path)

        wp_sign = _WP_SIGN_RE.search(fn_html)
        ajaxdata = _AJAXDATA_RE.search(fn_html)
        kdns = _KDNS_RE.search(fn_html)
        domain = _DOMAIN_RE.search(fn_html)
        if not (wp_sign and domain):
            return None

        ajax_url = domain.group(1)
        data = {
            "action": "downprocess",
            "websignkey": ajaxdata.group(1) if ajaxdata else "",
            "signs": ajaxdata.group(1) if ajaxdata else "",
            "sign": wp_sign.group(1),
            "websign": "",
            "kd": kdns.group(1) if kdns else "1",
            "ves": 1,
        }
        if password:
            data["p"] = password

        resp = await client.post(
            ajax_url,
            data=data,
            headers={
                "Referer": share_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            j = resp.json()
        except Exception:  # noqa: BLE001
            return None
        if j.get("zt") != 1:
            raise self._map_error(resp.status_code, j)
        dom = j.get("dom", "")
        path = j.get("url", "")
        if not dom or not path:
            return None
        direct_url = f"{dom}/file/{path}"
        name = (
            j.get("inf")
            or _extract_new_filename(share_html)
            or path
        )
        return {"direct_url": direct_url, "name": name}


def _extract_sign(html: str) -> str | None:
    m = _SIGN_RE.search(html)
    return m.group(1) if m else None


def _cookie_domain(url: str) -> str:
    """提取 URL 的注册域名（用于设置 cookie）。"""
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    # lanzouj.com 等二级域名，取最后两段作为 cookie 域。
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _extract_filename(html: str) -> str | None:
    m = _FILENAME_RE.search(html)
    if m:
        return m.group(1).strip()
    m = _TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        return title if title else None
    return None


def _extract_new_filename(html: str) -> str | None:
    """新协议页面文件名提取。"""
    m = _NEW_FILENAME_DIV_RE.search(html)
    if m:
        return m.group(1).strip()
    m = _NEW_FILENAME_RE.search(html)
    if m:
        return m.group(1).strip()
    return None
