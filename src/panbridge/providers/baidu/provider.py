"""百度网盘 Adapter（匿名解析分享列表 + 需 Cookie 直链下载）。

百度网盘分享链接可匿名解析文件列表（wxlist 接口），获取下载直链
需要有效 BDUSS Cookie（sharedownload 接口）。协议已公开（参考多个
MIT 开源实现，代码为本项目独立编写）。不依赖第三方 SDK。

核心协议：
1. 匿名解析列表：
   POST https://pan.baidu.com/share/wxlist?channel=weixin&version=2.2.2\
       &clienttype=25&web=1
   body: shorturl=<surl>&pwd=<pwd>&page=1&num=1000&order=time
   → data.seckey / shareid / uk / list[]
2. seckey 解码（base64url 变体转标准 base64）：
   seckey.replace('-','+').replace('~','=').replace('_','/')
3. 签名（匿名）：
   GET /share/tplconfig?surl=<surl>&fields=sign,timestamp&channel=chunlei\
       &web=1&app_id=250528&clienttype=0
   → data.sign / data.timestamp
4. 直链（需 Cookie）：
   POST /api/sharedownload?app_id=250528&channel=chunlei&clienttype=12\
       &sign=<sign>&timestamp=<ts>&web=1
   body: encrypt=0&extra={"sekey":"<randsk>"}&fid_list=[<fs_id>]\
       &primaryid=<shareid>&uk=<uk>&product=share&type=nolimit
   → list[0].dlink

能力边界（如实声明）：
- 「解析分享 + 文件列表」：匿名可用。
- 「获取直链」：需 BDUSS Cookie（登录）。
- 「转存」：需 Cookie，接口未验证，如实标注。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import (
    Account,
    Share,
    ShareFile,
    TransferResult,
)
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.providers.base import ShareProvider

_WXLIST_URL = (
    "https://pan.baidu.com/share/wxlist"
    "?channel=weixin&version=2.2.2&clienttype=25&web=1"
)
_TPL_CONFIG_URL = (
    "https://pan.baidu.com/share/tplconfig"
    "?fields=sign,timestamp&channel=chunlei&web=1&app_id=250528&clienttype=0"
)
_SHARE_DOWNLOAD_URL = (
    "https://pan.baidu.com/api/sharedownload"
    "?app_id=250528&channel=chunlei&clienttype=12&web=1"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
)


def decode_seckey(seckey: str) -> str:
    """base64url 变体转标准 base64（randsk）。"""
    return seckey.replace("-", "+").replace("~", "=").replace("_", "/")


class BaiduProvider(ShareProvider):
    name = "baidu"
    display_name = "百度网盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        self._cipher = cipher

    # ---- 内部工具 ----

    def _decrypt_credential(self, account: Account) -> str:
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "baidu account has no credential (cookie)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt cookie",
                retryable=False,
            )
        try:
            return self._cipher.decrypt(token)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "baidu credential could not be decrypted",
                retryable=False,
            ) from exc

    @staticmethod
    def _map_wxlist_error(data: dict[str, Any]) -> DomainError:
        errno = data.get("errno", 0)
        msg = str(data.get("show_msg", ""))
        # 常见 errno：-12 提取码错误，2/-7 分享不存在/已失效
        if errno in (-12, 105) or "提取码" in msg or "密码" in msg:
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "baidu share password incorrect"
            )
        if (
            errno in (2, -7, 110, 112, 118)
            or "失效" in msg
            or "不存在" in msg
            or "已删除" in msg
        ):
            return DomainError(ErrorCode.INVALID_SHARE, "baidu share invalid")
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"baidu wxlist error: errno={errno} {msg[:100]}",
            retryable=True,
        )

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("fs_id", "")),
            name=raw.get("server_filename", ""),
            is_dir=int(raw.get("isdir") or 0) == 1,
            size=int(raw.get("size", 0)) if raw.get("size") else None,
            parent_id=raw.get("path"),
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        data = await self._wxlist(share_id, password)
        files = [self._map_file(f) for f in data.get("list", [])]
        title = data.get("title", "") or f"baidu-share-{share_id}"
        return Share(
            provider=self.name,
            share_id=share_id,
            title=title,
            files=files,
            password=password,
            raw={
                "seckey": data.get("seckey"),
                "shareid": data.get("shareid"),
                "uk": data.get("uk"),
            },
        )

    async def _wxlist(
        self, share_id: str, password: str | None
    ) -> dict[str, Any]:
        surl = share_id if share_id.startswith("1") else "1" + share_id
        body = (
            f"&shorturl={surl}&dir=&root=1&pwd={password or ''}"
            "&page=1&num=1000&order=time"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _WXLIST_URL,
                content=body,
                headers={
                    "User-Agent": _UA,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"baidu wxlist returned {resp.status_code}",
            )
        data = resp.json()
        if data.get("errno") != 0:
            raise self._map_wxlist_error(data)
        return data.get("data", {})

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        data = await self._wxlist(share_id, password)
        return [self._map_file(f) for f in data.get("list", [])]

    async def validate_account(self, account: Account) -> bool:
        cookie = self._decrypt_credential(account)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                "https://pan.baidu.com/api/gettemplatevariable"
                "?fields=[%22bdstoken%22]&clienttype=0&web=1",
                headers={"User-Agent": _UA, "Cookie": cookie},
            )
        return resp.status_code == 200 and resp.json().get("errno") == 0

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
            "baidu share saving requires cookie (not yet verified)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "baidu account listing requires cookie (not yet verified)",
        )

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        """获取分享文件直链（需 Cookie）。

        file_id 为 share 文件列表中的 fs_id。需要 share 的 seckey/shareid/uk
        等上下文，这些在 inspect_share 时已解析。这里需要 share 上下文，
        因此实际下载链路应复用 resolve_share_dlink 传入完整上下文。
        """
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "baidu share download needs share context (use resolve_share_dlink)",
        )

    async def resolve_share_dlink(
        self,
        account: Account,
        surl: str,
        fs_id: str,
        shareid: str,
        uk: str,
        seckey: str,
    ) -> str:
        """解析分享文件直链（需 Cookie）。"""
        cookie = self._decrypt_credential(account)
        randsk = decode_seckey(seckey)
        if "%" in randsk:
            randsk = urllib.parse.unquote(randsk)

        # 获取签名
        surl_full = surl if surl.startswith("1") else "1" + surl
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _TPL_CONFIG_URL + f"&surl={surl_full}",
                headers={"User-Agent": "netdisk;pan.baidu.com", "Cookie": cookie},
            )
            tpl = resp.json()
            if tpl.get("errno") != 0:
                raise self._map_wxlist_error(tpl)
            sign = tpl["data"]["sign"]
            timestamp = tpl["data"]["timestamp"]

            # 获取直链
            body = (
                "encrypt=0"
                "&extra=" + urllib.parse.quote('{"sekey":"' + randsk + '"}')
                + f"&fid_list=[{fs_id}]"
                + f"&primaryid={shareid}"
                + f"&uk={uk}"
                + "&product=share&type=nolimit"
            )
            resp2 = await client.post(
                _SHARE_DOWNLOAD_URL
                + f"&sign={sign}&timestamp={timestamp}",
                content=body,
                headers={
                    "User-Agent": _UA,
                    "Cookie": cookie,
                    "Referer": "https://pan.baidu.com/disk/home",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            dl = resp2.json()
            if dl.get("errno") != 0:
                errmsg = dl.get("errmsg", "")
                if "验证" in errmsg or "风控" in errmsg or "verify" in errmsg.lower():
                    raise DomainError(
                        ErrorCode.ACTION_REQUIRED,
                        "baidu requires verification (risk control)",
                        retryable=False,
                    )
                raise DomainError(
                    ErrorCode.DOWNLOAD_FAILED,
                    f"baidu download error: {errmsg[:100]}",
                    retryable=True,
                )
            items = dl.get("list", [])
            if not items:
                raise DomainError(
                    ErrorCode.DOWNLOAD_FAILED,
                    "baidu returned empty download list",
                    retryable=True,
                )
            return items[0].get("dlink", "")
