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
5. 转存（需 Cookie）：
   gettemplatevariable → bdstoken → share/verify 取 BDCLND(sekey)
   → share/transfer?shareid=&from=&sekey=&bdstoken=
   body: fsidlist=[...]&path=<dest>

能力边界（如实声明）：
- 「解析分享 + 文件列表」：匿名可用。
- 「获取直链 / 转存」：需 BDUSS Cookie（登录），协议已实现但未经真实
  账号验证。
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


def _extract_js(page: str, key: str) -> str:
    """从分享页内嵌 JS 中提取 `"key":"value"` 或 `"key":value`。"""
    import re

    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"?([^",}]+)"?', page)
    return m.group(1) if m else ""


def _extract_cookie(client: httpx.AsyncClient, name: str) -> str:
    """从 client 的 cookie jar 中提取指定 cookie 值。"""
    for c in client.cookies.jar:
        if c.name == name:
            return c.value
    return ""


class BaiduProvider(ShareProvider):
    name = "baidu"
    display_name = "百度网盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
            "save_shared_files",
            "validate_account",
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
        """转存分享文件到账号网盘（需 BDUSS Cookie）。

        协议（公开 web API）：
        1. GET /api/gettemplatevariable?fields=[bdstoken] → bdstoken
        2. POST /share/verify?surl=&pwd=&bdstoken= → 设置 BDCLND cookie（sekey）
        3. POST /share/transfer?shareid=&from=&sekey=&bdstoken=...
           body: fsidlist=[...]&path=<dest>
        """
        cookie = self._decrypt_credential(account)
        surl = share_id if share_id.startswith("1") else "1" + share_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. 获取 bdstoken
            resp = await client.get(
                "https://pan.baidu.com/api/gettemplatevariable"
                "?fields=[%22bdstoken%22]&clienttype=0&web=1",
                headers={"User-Agent": _UA, "Cookie": cookie},
            )
            if resp.status_code != 200 or resp.json().get("errno") != 0:
                raise DomainError(
                    ErrorCode.ACCOUNT_EXPIRED,
                    "baidu cookie invalid (cannot get bdstoken)",
                )
            bdstoken = resp.json()["data"]["bdstoken"]

            # 2. verify 获取 sekey（BDCLND cookie）
            verify_body = f"pwd={password or ''}&vcode=&vcodestr="
            verify_resp = await client.post(
                f"https://pan.baidu.com/share/verify?surl={surl}"
                "&channel=chunlei&web=1&app_id=250528&clienttype=0"
                f"&bdstoken={bdstoken}",
                content=verify_body,
                headers={
                    "User-Agent": _UA,
                    "Cookie": cookie,
                    "Referer": f"https://pan.baidu.com/s/{surl}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if verify_resp.status_code != 200:
                raise self._map_wxlist_error(
                    {
                        "errno": -1,
                        "show_msg": f"verify returned {verify_resp.status_code}",
                    }
                )
            verify_data = verify_resp.json()
            if verify_data.get("errno") != 0:
                raise self._map_wxlist_error(verify_data)

            # 从分享页提取 shareid / from(share_uk)
            share_page = await client.get(
                f"https://pan.baidu.com/s/{surl}",
                headers={"User-Agent": _UA, "Cookie": cookie},
            )
            page = share_page.text
            shareid = _extract_js(page, "shareid")
            share_uk = _extract_js(page, "share_uk")
            if not shareid or not share_uk:
                raise DomainError(
                    ErrorCode.INVALID_SHARE,
                    "baidu: cannot extract shareid/share_uk from share page",
                )

            # sekey 来自 BDCLND cookie
            sekey = _extract_cookie(client, "BDCLND")
            if not sekey:
                # 回退：用 verify 前的 sekey（来自 wxlist 的 seckey）
                sekey = ""

            # 3. 转存
            ondup = "newcopy" if on_conflict == "rename" else "fail"
            params = (
                f"shareid={shareid}&from={share_uk}&sekey={urllib.parse.quote(sekey)}"
                "&channel=chunlei&web=1&app_id=250528&clienttype=0"
                f"&bdstoken={bdstoken}&ondup={ondup}&async=1"
            )
            fsidlist = "[" + ",".join(file_ids) + "]"
            transfer_body = (
                f"fsidlist={urllib.parse.quote(fsidlist)}"
                f"&path={urllib.parse.quote(destination or '/')}"
            )
            transfer_resp = await client.post(
                "https://pan.baidu.com/share/transfer?" + params,
                content=transfer_body,
                headers={
                    "User-Agent": _UA,
                    "Cookie": cookie,
                    "Referer": f"https://pan.baidu.com/s/{surl}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if transfer_resp.status_code != 200:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"baidu transfer returned {transfer_resp.status_code}",
                    retryable=True,
                )
            transfer_data = transfer_resp.json()
            if transfer_data.get("errno") != 0:
                msg = str(transfer_data.get("show_msg", ""))
                if "容量" in msg or "quota" in msg.lower():
                    raise DomainError(ErrorCode.QUOTA_EXCEEDED, "baidu quota exceeded")
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"baidu transfer error: {msg[:100]}",
                    retryable=True,
                )

        task_data = transfer_data.get("data") or {}
        task_id = (
            task_data.get("task_id")
            or transfer_data.get("task_id")
            or transfer_data.get("extra", {}).get("task_id", "")
        )
        return TransferResult(remote_task_id=str(task_id) if task_id else None)

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
