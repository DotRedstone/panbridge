"""中国移动云盘（和彩云）Adapter。

基于已公开的移动云盘 web 协议（参考 AList 的 139 驱动、Yun139 库
`cloud_driver_sdk` 等多个开源实现，代码为本项目独立编写）。不依赖第三方
SDK。

移动云盘登录态通过 `Authorization: Basic <token>` 传递（token 为 Basic
后的内容），可从浏览器 DevTools 的 `hcy/file/list` 请求头获取，或通过
账号密码 + 短信验证码登录。

关键协议端点（已公开，但未经真实验证）：
- 文件列表：`GET https://yun.139.com/hcy/file/list`（旧版）或
  `GET https://yun.139.com/api/file/list`（新版），参数含 parentFileId、
  cursor 分页。
- 下载直链：`GET https://yun.139.com/api/file/download`，参数 fileId，
  返回 url。
- 分享链接：`https://yun.139.com/shareweb/#/w/i/<linkID>`。

能力边界（如实声明）：
- 「账号文件列表 / 下载」：需 Authorization，端点已实现但未经真实账号
  验证（端点 URL 可能随版本变化）。
- 「分享解析 / 转存」：分享页走独立 caiyun.139.com 通道，协议未验证。
"""

from __future__ import annotations

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

_API = "https://yun.139.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


class CaiyunProvider(ShareProvider):
    name = "caiyun"
    display_name = "中国移动云盘"
    capabilities = frozenset(
        {
            "validate_account",
            "list_account_files",
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
                "caiyun account has no credential (authorization token)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt token",
                retryable=False,
            )
        try:
            return self._cipher.decrypt(token)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "caiyun credential could not be decrypted",
                retryable=False,
            ) from exc

    def _auth_headers(self, account: Account) -> dict[str, str]:
        token = self._decrypt_credential(account)
        return {**_HEADERS, "Authorization": f"Basic {token}"}

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("fileId") or raw.get("fid") or ""),
            name=raw.get("name") or raw.get("fileName") or "",
            is_dir=raw.get("type") == "folder"
            or raw.get("isDir") is True,
            size=raw.get("size") or raw.get("fileSize"),
            parent_id=str(raw.get("parentFileId") or ""),
        )

    @staticmethod
    def _map_error(resp: httpx.Response, data: dict[str, Any]) -> DomainError:
        code = data.get("code") or data.get("resultCode")
        msg = str(data.get("message") or data.get("resultDesc") or "")
        if resp.status_code in (401, 403) or "token" in msg.lower():
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "caiyun authorization invalid or expired",
                retryable=False,
            )
        if code == "0000" or code == 0 or code == "0":
            # 成功，但此函数不应在成功时被调用
            pass
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"caiyun api error: {msg or code or resp.status_code}",
            retryable=True,
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "caiyun share inspection not supported (unverified protocol)",
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "caiyun share listing not supported (unverified protocol)",
        )

    async def validate_account(self, account: Account) -> bool:
        """用账号信息接口校验 Authorization 是否有效。"""
        headers = self._auth_headers(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _API + "/api/user/info", headers=headers
                )
            return resp.status_code == 200
        except httpx.HTTPError:  # noqa: BLE001
            return False

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
            "caiyun share saving not supported (unverified protocol)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        headers = self._auth_headers(account)
        params = {
            "parentFileId": parent_id or "/",
            "pageSize": "100",
            "pageNum": "1",
            "orderBy": "lastOpTime",
            "descending": "true",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _API + "/api/file/list", params=params, headers=headers
                )
        except httpx.HTTPError as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun network error: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        if resp.status_code != 200:
            raise self._map_error(resp, {})
        data = resp.json()
        if data.get("code") not in ("0000", 0, "0", None):
            raise self._map_error(resp, data)
        items = data.get("items") or data.get("data", {}).get("items", [])
        return [self._map_file(it) for it in items if isinstance(it, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        headers = self._auth_headers(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _API + "/api/file/download",
                    params={"fileId": file_id},
                    headers=headers,
                )
        except httpx.HTTPError as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun network error: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        if resp.status_code != 200:
            raise self._map_error(resp, {})
        data = resp.json()
        url = data.get("url") or data.get("cdnUrl")
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "caiyun returned empty download url",
                retryable=True,
            )
        return {
            "url": url,
            "name": data.get("name") or f"{file_id}.bin",
            "size": data.get("size"),
            "checksum": None,
            "headers": {},
        }
