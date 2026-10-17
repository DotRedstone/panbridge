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
    "ilanzou.com",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

# 从分享页提取 sign 参数：action=downprocess&sign=xxx&p=
_SIGN_RE = re.compile(r"action=downprocess&sign=([A-Za-z0-9_\-]+?)&p=")
# 页面脚本中的 ajax 接口：url : '/ajaxm.php?file=123'
_AJAX_RE = re.compile(r"url\s*:\s*'(/ajaxm\.php\?file=\d+)'")
# 文件名（优先从标题/页面元素提取）
_TITLE_RE = re.compile(
    r"<title>\s*(.*?)\s*[-–|]?\s*蓝奏云.*?</title>", re.S | re.I
)
_FILENAME_RE = re.compile(
    r"class=\"n_box\".*?<b>(.*?)</b>", re.S
)


class LanzouProvider(ShareProvider):
    name = "lanzou"
    display_name = "蓝奏云"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
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
        info = str(data.get("info", ""))
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
        """解析单文件分享页，返回 {direct_url, name}。"""
        url = self._share_url(share_id)
        async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    f"lanzou share page returned {resp.status_code}",
                )
            html = resp.text

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


def _extract_sign(html: str) -> str | None:
    m = _SIGN_RE.search(html)
    return m.group(1) if m else None


def _extract_filename(html: str) -> str | None:
    m = _FILENAME_RE.search(html)
    if m:
        return m.group(1).strip()
    m = _TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        return title if title else None
    return None
