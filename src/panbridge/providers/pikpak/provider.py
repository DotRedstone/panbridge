"""PikPak Adapter（匿名分享解析 + 直链下载）。

PikPak 分享链接可匿名解析并下载，协议已公开（参考 AList 的 pikpak_share
驱动与多个 MIT 开源实现，代码为本项目独立编写）。不依赖第三方 SDK。

核心协议：
1. 计算 captcha_sign（MD5 链）：
   str = client_id + client_version + package_name + device_id + timestamp
   对每个 algorithm salt 依次 str = md5(str + salt)
   sign = "1." + str
2. POST https://user.mypikpak.com/v1/shield/captcha/init
   body={client_id, device_id, captcha_token, action, meta:{...}}
   → captcha_token
3. GET /drive/v1/share?share_id&pass_code → pass_code_token
4. GET /drive/v1/share/detail?share_id&pass_code_token&parent_id → 文件列表
5. GET /drive/v1/share/file_info?share_id&file_id&pass_code_token → web_content_link

能力边界（如实声明）：
- 「解析分享 + 文件列表 + 直链下载」匿名可用（无需登录）。
- 「转存」需登录账号（access_token），未验证，如实标注。
- 需住宅 IP（IDC 段可能被屏蔽，返回 PROHIBITED）。
"""

from __future__ import annotations

import hashlib
import time
import uuid
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

_API = "https://api-drive.mypikpak.com"
_USER_API = "https://user.mypikpak.com"

# Web 平台常量（公开协议参数）
_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
_CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"
_CLIENT_VERSION = "2.0.0"
_PACKAGE_NAME = "mypikpak.com"

_WEB_ALGORITHMS = [
    "C9qPpZLN8ucRTaTiUMWYS9cQvWOE",
    "+r6CQVxjzJV6LCV",
    "F",
    "pFJRC",
    "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt",
    "/750aCr4lm/Sly/c",
    "RB+DT/gZCrbV",
    "",
    "CyLsf7hdkIRxRm215hl",
    "7xHvLi2tOYP0Y92b",
    "ZGTXXxu8E/MIWaEDB+Sm/",
    "1UI3",
    "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO",
    "ihtqpG6FMt65+Xk+tWUH2",
    "NhXXU9rg4XXdzo7u5o",
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Client-ID": _CLIENT_ID,
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def captcha_sign(client_id: str, version: str, package: str, device_id: str,
                 timestamp: str) -> str:
    """计算 PikPak captcha 签名（MD5 链）。"""
    s = client_id + version + package + device_id + timestamp
    for salt in _WEB_ALGORITHMS:
        s = _md5(s + salt)
    return "1." + s


class PikPakProvider(ShareProvider):
    name = "pikpak"
    display_name = "PikPak"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
            "get_share_download_info",
        }
    )

    def __init__(self) -> None:
        self._device_id = uuid.uuid4().hex

    # ---- 内部工具 ----

    async def _get_captcha_token(self, action: str) -> str:
        timestamp = str(int(time.time() * 1000))
        sign = captcha_sign(
            _CLIENT_ID, _CLIENT_VERSION, _PACKAGE_NAME, self._device_id, timestamp
        )
        body = {
            "action": action,
            "captcha_token": "",
            "client_id": _CLIENT_ID,
            "device_id": self._device_id,
            "meta": {
                "timestamp": timestamp,
                "captcha_sign": sign,
                "client_version": _CLIENT_VERSION,
                "package_name": _PACKAGE_NAME,
                "user_id": "",
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _USER_API + "/v1/shield/captcha/init",
                json=body,
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"pikpak captcha init returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        token = data.get("captcha_token", "")
        if not token:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "pikpak captcha token empty",
                retryable=True,
            )
        return token

    async def _get_pass_code_token(
        self, share_id: str, password: str | None, captcha_token: str
    ) -> str:
        params = {"share_id": share_id, "pass_code": password or ""}
        headers = {**_HEADERS, "X-Captcha-Token": captcha_token}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _API + "/drive/v1/share", params=params, headers=headers
            )
        if resp.status_code != 200:
            raise self._map_status(resp)
        data = resp.json()
        if data.get("share_status") == "PASS_CODE_ERROR":
            raise DomainError(ErrorCode.INVALID_PASSWORD, "pikpak pass code incorrect")
        if data.get("share_status") not in ("OK", "PASS_CODE_EMPTY"):
            status = data.get("share_status", "")
            if status == "PROHIBITED":
                raise DomainError(
                    ErrorCode.RATE_LIMITED,
                    "pikpak access prohibited (IDC IP blocked?)",
                )
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"pikpak share status: {status or data.get('share_status_text')}",
            )
        return data.get("pass_code_token", "")

    @staticmethod
    def _map_status(resp: httpx.Response) -> DomainError:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        err = data.get("error") or {}
        code = err.get("code")
        if code == 9:
            return DomainError(
                ErrorCode.RATE_LIMITED, "pikpak captcha expired", retryable=True
            )
        if code in (16, 10) or resp.status_code == 429:
            return DomainError(
                ErrorCode.RATE_LIMITED, "pikpak rate limited", retryable=True
            )
        if resp.status_code in (401, 403):
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED, "pikpak token invalid", retryable=False
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"pikpak api error: {resp.status_code}",
            retryable=True,
        )

    @staticmethod
    def _map_share_item(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            is_dir=raw.get("kind", "") == "drive#folder",
            size=int(raw.get("size", 0)) if raw.get("size") else None,
            parent_id=str(raw.get("parent_id", "")),
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        captcha = await self._get_captcha_token("GET:/drive/v1/share")
        pass_token = await self._get_pass_code_token(share_id, password, captcha)
        files = await self._list_files(share_id, pass_token, captcha, "0")
        return Share(
            provider=self.name,
            share_id=share_id,
            title=f"pikpak-share-{share_id}",
            files=files,
            password=password,
            raw={"pass_code_token": pass_token},
        )

    async def _list_files(
        self, share_id: str, pass_token: str, captcha: str, parent_id: str
    ) -> list[ShareFile]:
        params = {
            "parent_id": parent_id,
            "share_id": share_id,
            "thumbnail_size": "SIZE_LARGE",
            "with_audit": "true",
            "limit": "100",
            "page_token": "",
            "pass_code_token": pass_token,
        }
        headers = {**_HEADERS, "X-Captcha-Token": captcha}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _API + "/drive/v1/share/detail", params=params, headers=headers
            )
        if resp.status_code != 200:
            raise self._map_status(resp)
        data = resp.json()
        return [self._map_share_item(f) for f in data.get("files", [])]

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        captcha = await self._get_captcha_token("GET:/drive/v1/share")
        pass_token = await self._get_pass_code_token(share_id, password, captcha)
        return await self._list_files(
            share_id, pass_token, captcha, parent_id or "0"
        )

    async def validate_account(self, account: Account) -> bool:
        # PikPak 匿名分享无需账号
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
            "pikpak share saving requires login (not yet verified)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "pikpak account listing requires login (not yet verified)",
        )

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        # PikPak 账号文件下载需要登录态 access_token，未验证，如实拒绝。
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "pikpak account download requires login (not yet verified)",
        )

    async def get_share_download_info(
        self,
        share_id: str,
        password: str | None,
        file_id: str,
        file: ShareFile | None = None,
        account: Account | None = None,
    ) -> dict[str, Any]:
        """解析分享中单个文件的直链（匿名，无需登录）。

        重新解析 captcha/pass_token，然后通过 file_info 接口拿到
        web_content_link。`file` 参数提供 name/size（inspect 已解析），
        避免返回空元数据；下载所需的 pass_token/captcha 在本次调用内
        重新获取（临时凭据，不长期缓存）。
        """
        captcha = await self._get_captcha_token("GET:/drive/v1/share")
        pass_token = await self._get_pass_code_token(share_id, password, captcha)
        link = await self.resolve_file_download_url(
            share_id, file_id, pass_token, captcha
        )
        name = file.name if file is not None else None
        size = file.size if file is not None else None
        return {
            "url": link,
            "name": name,
            "size": size,
            "checksum": None,
            "headers": {"User-Agent": _HEADERS["User-Agent"]},
        }

    async def resolve_file_download_url(
        self, share_id: str, file_id: str, pass_token: str, captcha: str
    ) -> str:
        """解析分享中单个文件的直链（匿名）。"""
        params = {
            "share_id": share_id,
            "file_id": file_id,
            "pass_code_token": pass_token,
        }
        headers = {**_HEADERS, "X-Captcha-Token": captcha}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _API + "/drive/v1/share/file_info", params=params, headers=headers
            )
        if resp.status_code != 200:
            raise self._map_status(resp)
        data = resp.json()
        info = data.get("file_info", {})
        link = info.get("web_content_link", "")
        if not link:
            # 视频文件可能在 medias[].link
            for media in info.get("medias", []) or []:
                if media.get("link", {}).get("url"):
                    link = media["link"]["url"]
                    break
        if not link:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "pikpak returned empty download link",
                retryable=True,
            )
        return link
