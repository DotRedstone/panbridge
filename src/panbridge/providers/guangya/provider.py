"""光鸭云盘 Adapter。

光鸭云盘（guangyapan.com）分享解析与转存协议参考 pan-relay（MIT 许可）的
`guangya_client.py`，代码为本项目独立编写，不依赖第三方 SDK。光鸭云盘
API 结构近似 Google Drive 风格（`drive/v1/...`），分享解析匿名可用，
转存 / 账号文件列表 / 下载需登录态（access_token，可用 refresh_token
刷新）。

关键协议端点：
1. 刷新 access_token：
   POST https://account.guangyapan.com/v1/auth/token
   body={grant_type:"refresh_token", refresh_token, client_id}
   → access_token / expires_in
2. 解析分享（匿名可用）：
   GET https://api.guangyapan.com/drive/v1/share?share_id=&pass_code=
   → files[]（字段 id / name / kind / size）+ pass_code_token
3. 转存（需登录）：
   POST https://api.guangyapan.com/drive/v1/share/restore
   body={share_id, pass_code_token, file_ids, parent_id}
   → file_ids（转存后的新文件 ID）
4. 账号文件列表（需登录）：
   GET https://api.guangyapan.com/drive/v1/files?parent_id=&limit=
   → files[]（kind 为 drive#folder 表示目录）
5. 创建目录（需登录）：
   POST https://api.guangyapan.com/drive/v1/files
   body={kind:"drive#folder", name, parent_id} → id
6. 下载直链（需登录）：
   GET https://api.guangyapan.com/drive/v1/files/{file_id}/download
   （返回下载 URL 或 302，具体以真实响应为准）

能力边界（如实声明）：
- 「解析分享」匿名可用（无需登录）。
- 「转存 / 账号列表 / 下载 / 创建目录」需 access_token，协议已实现但
  未经真实账号验证。
"""

from __future__ import annotations

import json
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

_API_BASE = "https://api.guangyapan.com"
_AUTH_BASE = "https://account.guangyapan.com"
_CLIENT_ID = "aMe-8VSlkrbQXpUR"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.guangyapan.com",
    "Referer": "https://www.guangyapan.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class GuangyaProvider(ShareProvider):
    name = "guangya"
    display_name = "光鸭云盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "validate_account",
            "get_download_info",
            "create_folder",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        self._cipher = cipher

    # ---- 内部工具 ----

    def _decrypt_credential(self, account: Account) -> dict[str, str]:
        """解密账号凭据，返回 {access_token, refresh_token} 字典。

        凭据可为纯 access_token 字符串，或包含 access_token /
        refresh_token 的 JSON 字符串，加密后入库。
        """
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "guangya account has no credential",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt",
                retryable=False,
            )
        try:
            plain = self._cipher.decrypt(token)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "guangya credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

        cred: dict[str, str] = {"access_token": "", "refresh_token": ""}
        stripped = plain.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    cred["access_token"] = str(
                        data.get("access_token") or ""
                    ).strip()
                    cred["refresh_token"] = str(
                        data.get("refresh_token") or ""
                    ).strip()
            except json.JSONDecodeError:
                cred["access_token"] = stripped
        else:
            cred["access_token"] = stripped
        if not cred["access_token"] and not cred["refresh_token"]:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "guangya credential is empty",
            )
        return cred

    async def _resolve_token(self, account: Account) -> str:
        """返回可用的 access_token，必要时用 refresh_token 刷新。"""
        cred = self._decrypt_credential(account)
        if cred["access_token"]:
            return cred["access_token"]
        if cred["refresh_token"]:
            url = f"{_AUTH_BASE}/v1/auth/token"
            body = {
                "grant_type": "refresh_token",
                "refresh_token": cred["refresh_token"],
                "client_id": _CLIENT_ID,
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=body, headers=_HEADERS)
            if resp.status_code != 200:
                raise DomainError(
                    ErrorCode.ACCOUNT_EXPIRED,
                    "guangya refresh_token failed",
                    retryable=False,
                )
            data = resp.json()
            token = data.get("access_token") or data.get("token") or ""
            if not token:
                raise DomainError(
                    ErrorCode.ACCOUNT_EXPIRED,
                    "guangya refresh_token returned no access_token",
                    retryable=False,
                )
            return token
        raise DomainError(
            ErrorCode.ACCOUNT_EXPIRED,
            "guangya account has no usable credential",
        )

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            is_dir=raw.get("kind") == "drive#folder" or bool(raw.get("is_dir")),
            size=raw.get("size"),
            parent_id=str(raw.get("parent_id") or ""),
        )

    @staticmethod
    def _map_error(data: dict[str, Any], status_code: int = 200) -> DomainError:
        msg = str(
            data.get("message") or data.get("msg") or data.get("error") or ""
        )
        error_code = str(data.get("error_code") or "")
        low = f"{error_code} {msg}".lower()
        if any(
            k in low
            for k in ["pass_code", "passcode", "password", "密码", "提取码"]
        ):
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "guangya share password incorrect"
            )
        if any(
            k in low
            for k in [
                "not_found",
                "expired",
                "deleted",
                "不存在",
                "失效",
                "删除",
                "过期",
            ]
        ):
            return DomainError(ErrorCode.INVALID_SHARE, "guangya share invalid")
        if status_code == 401 or "unauthorized" in low or "token" in low:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "guangya access_token invalid or expired",
                retryable=False,
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"guangya api error: {msg or error_code or status_code}",
            retryable=True,
        )

    async def _get_share_detail(
        self, share_id: str, password: str | None
    ) -> dict[str, Any]:
        """匿名获取分享详情，返回原始 JSON。"""
        url = f"{_API_BASE}/drive/v1/share"
        params = {"share_id": share_id, "pass_code": password or ""}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params, headers=_HEADERS)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"guangya share list returned {resp.status_code}",
            )
        data = resp.json()
        if data.get("error_code") or data.get("code", 0) not in (0, 200, None):
            raise self._map_error(data, resp.status_code)
        return data

    @staticmethod
    def _extract_files(detail: dict[str, Any]) -> list[dict[str, Any]]:
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        files = data.get("files") or []
        return [f for f in files if isinstance(f, dict)]

    async def _request_with_token(
        self,
        account: Account,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._resolve_token(account)
        headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
        url = f"{_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, params=params, json=payload, headers=headers
            )
        if resp.status_code not in (200, 201):
            raise self._map_error(
                self._safe_json(resp), resp.status_code
            )
        return resp.json()

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"message": resp.text[:200]}

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        detail = await self._get_share_detail(share_id, password)
        files = [self._map_file(f) for f in self._extract_files(detail)]
        return Share(
            provider=self.name,
            share_id=share_id,
            title=f"guangya-share-{share_id}",
            files=files,
            password=password,
            raw=detail,
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        detail = await self._get_share_detail(share_id, password)
        files = self._extract_files(detail)
        if parent_id:
            files = [f for f in files if str(f.get("parent_id")) == parent_id]
        return [self._map_file(f) for f in files]

    async def validate_account(self, account: Account) -> bool:
        try:
            await self._resolve_token(account)
            data = await self._request_with_token(
                account, "GET", "/drive/v1/files", params={"limit": 1}
            )
            return isinstance(data, dict)
        except DomainError:
            raise
        except Exception:  # noqa: BLE001
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
        detail = await self._get_share_detail(share_id, password)
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        pass_code_token = data.get("pass_code_token") or ""
        result = await self._request_with_token(
            account,
            "POST",
            "/drive/v1/share/restore",
            payload={
                "share_id": share_id,
                "pass_code_token": pass_code_token,
                "file_ids": file_ids,
                "parent_id": destination if destination not in ("/", "", None) else "0",
            },
        )
        saved = (
            result.get("file_ids")
            or result.get("data", {}).get("file_ids")
            or file_ids
        )
        return TransferResult(saved_file_ids=[str(i) for i in saved])

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        result = await self._request_with_token(
            account,
            "POST",
            "/drive/v1/files",
            payload={"kind": "drive#folder", "name": name, "parent_id": parent_id},
        )
        fid = result.get("id") or result.get("data", {}).get("id")
        if not fid:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "guangya create_folder returned no id",
                retryable=False,
            )
        return str(fid)

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        data = await self._request_with_token(
            account,
            "GET",
            "/drive/v1/files",
            params={"parent_id": parent_id or "0", "limit": 100},
        )
        files = data.get("files") or data.get("data", {}).get("files") or []
        return [self._map_file(f) for f in files if isinstance(f, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        token = await self._resolve_token(account)
        headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
        url = f"{_API_BASE}/drive/v1/files/{file_id}/download"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code not in (200, 201, 302):
            raise self._map_error(self._safe_json(resp), resp.status_code)
        # 响应可能直接返回下载 URL，或 JSON 中携带 url 字段
        if resp.headers.get("location"):
            download_url = resp.headers["location"]
        else:
            try:
                data = resp.json()
                download_url = (
                    data.get("url")
                    or data.get("download_url")
                    or data.get("data", {}).get("url")
                    or ""
                )
            except Exception:  # noqa: BLE001
                download_url = ""
        if not download_url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "guangya returned empty download url",
                retryable=True,
            )
        return {
            "url": download_url,
            "name": f"{file_id}.bin",
            "size": None,
            "checksum": None,
            "headers": {"Referer": "https://www.guangyapan.com/"},
        }
