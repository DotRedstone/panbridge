"""悟空网盘 Adapter。

悟空网盘（pan.wkbrowser.com，悟空浏览器网盘）分享解析与转存协议参考
pan-relay（MIT 许可）的 `wukong_client.py`，代码为本项目独立编写，不依赖
第三方 SDK。分享解析匿名可用，转存 / 账号文件列表 / 下载需登录态（Cookie
或 Bearer token）。

关键协议端点（`https://pan.wkbrowser.com`）：
1. 解析分享（匿名可用）：
   GET /api/v1/share/info?share_id=&pwd=
   → data.files[]（字段 file_id / name / title）+ data.title
2. 转存（需登录）：
   POST /api/v1/share/save
   body={share_id, file_ids, target_dir_id} → data.file_ids
3. 创建目录（需登录）：
   POST /api/v1/folder/create body={name, parent_id} → data.file_id
4. 文件信息（需登录）：
   GET /api/v1/file/info?file_id= → data.name / title
5. 下载直链（需登录）：
   GET /api/v1/file/download?file_id=（端点以真实响应为准）

能力边界（如实声明）：
- 「解析分享」匿名可用（无需登录）。
- 「转存 / 账号列表 / 下载 / 创建目录」需登录态，协议已实现但未经
  真实账号验证。
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

_API_BASE = "https://pan.wkbrowser.com"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://pan.wkbrowser.com",
    "Referer": "https://pan.wkbrowser.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class WukongProvider(ShareProvider):
    name = "wukong"
    display_name = "悟空网盘"
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
        """解密账号凭据，返回 {cookie, token} 字典。

        凭据可为纯 Cookie 字符串，或包含 cookie / token / access_token
        的 JSON 字符串，加密后入库。
        """
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "wukong account has no credential",
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
                "wukong credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

        cred: dict[str, str] = {"cookie": "", "token": ""}
        stripped = plain.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    cred["cookie"] = str(
                        data.get("cookie") or data.get("Cookie") or ""
                    ).strip()
                    cred["token"] = str(
                        data.get("token") or data.get("access_token") or ""
                    ).strip()
            except json.JSONDecodeError:
                cred["cookie"] = stripped
        else:
            cred["cookie"] = stripped
        if not cred["cookie"] and not cred["token"]:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "wukong credential is empty",
            )
        return cred

    def _auth_headers(self, account: Account) -> dict[str, str]:
        cred = self._decrypt_credential(account)
        headers = dict(_HEADERS)
        if cred["cookie"]:
            headers["Cookie"] = cred["cookie"]
        if cred["token"]:
            headers["Authorization"] = f"Bearer {cred['token']}"
        return headers

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        file_id = raw.get("file_id") or raw.get("id") or ""
        return ShareFile(
            file_id=str(file_id),
            name=raw.get("name") or raw.get("title") or "",
            is_dir=bool(raw.get("is_dir") or raw.get("is_folder")),
            size=raw.get("size"),
            parent_id=str(raw.get("parent_id") or raw.get("target_dir_id") or ""),
        )

    @staticmethod
    def _map_error(data: dict[str, Any], status_code: int = 200) -> DomainError:
        msg = str(data.get("message") or data.get("msg") or "")
        if any(k in msg for k in ["密码", "提取码", "口令", "access code", "passcode"]):
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "wukong share password incorrect"
            )
        if any(k in msg for k in ["不存在", "失效", "违规", "删除", "过期", "取消"]):
            return DomainError(ErrorCode.INVALID_SHARE, "wukong share invalid")
        if status_code == 401 or "unauthorized" in msg.lower():
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "wukong credential invalid or expired",
                retryable=False,
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"wukong api error: {msg or status_code}",
            retryable=True,
        )

    async def _get_share_detail(
        self, share_id: str, password: str | None
    ) -> dict[str, Any]:
        """匿名获取分享详情，返回原始 JSON。"""
        url = f"{_API_BASE}/api/v1/share/info"
        params = {"share_id": share_id, "pwd": password or ""}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params, headers=_HEADERS)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"wukong share info returned {resp.status_code}",
            )
        data = resp.json()
        if data.get("code") not in (0, 200, None):
            raise self._map_error(data, resp.status_code)
        return data

    @staticmethod
    def _extract_files(detail: dict[str, Any]) -> list[dict[str, Any]]:
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        files = data.get("files") or data.get("file_list") or []
        return [f for f in files if isinstance(f, dict)]

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"message": resp.text[:200]}

    async def _request_with_auth(
        self,
        account: Account,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._auth_headers(account)
        url = f"{_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, params=params, json=payload, headers=headers
            )
        if resp.status_code not in (200, 201):
            raise self._map_error(self._safe_json(resp), resp.status_code)
        data = resp.json()
        if data.get("code") not in (0, 200, None):
            raise self._map_error(data, resp.status_code)
        return data

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        detail = await self._get_share_detail(share_id, password)
        data = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        title = data.get("title") or data.get("name") or f"wukong-share-{share_id}"
        files = [self._map_file(f) for f in self._extract_files(detail)]
        return Share(
            provider=self.name,
            share_id=share_id,
            title=str(title),
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
            data = await self._request_with_auth(
                account, "GET", "/api/v1/file/list", params={"parent_id": "0"}
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
        result = await self._request_with_auth(
            account,
            "POST",
            "/api/v1/share/save",
            payload={
                "share_id": share_id,
                "file_ids": file_ids,
                "target_dir_id": (
                    destination if destination not in ("/", "", None) else "0"
                ),
            },
        )
        saved = (
            result.get("file_ids")
            or result.get("data", {}).get("file_ids")
        )
        if not saved:
            # 平台未返回真实新 fid：不伪造（禁止用源 fid 冒充），
            # 返回空映射交由 Worker 走 resolve 兜底或如实报错。
            return TransferResult(saved_file_ids=[])
        return TransferResult(saved_file_ids=[str(i) for i in saved])

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        result = await self._request_with_auth(
            account,
            "POST",
            "/api/v1/folder/create",
            payload={"name": name, "parent_id": parent_id},
        )
        fid = result.get("file_id") or result.get("id")
        if not fid:
            fid = result.get("data", {}).get("file_id") or result.get(
                "data", {}
            ).get("id")
        if not fid:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "wukong create_folder returned no id",
                retryable=False,
            )
        return str(fid)

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        data = await self._request_with_auth(
            account,
            "GET",
            "/api/v1/file/list",
            params={"parent_id": parent_id or "0"},
        )
        files = (
            data.get("files")
            or data.get("file_list")
            or data.get("data", {}).get("files")
            or []
        )
        return [self._map_file(f) for f in files if isinstance(f, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        data = await self._request_with_auth(
            account,
            "GET",
            "/api/v1/file/download",
            params={"file_id": file_id},
        )
        info = data.get("data") if isinstance(data.get("data"), dict) else data
        download_url = (
            info.get("url") or info.get("download_url") or data.get("url") or ""
        )
        if not download_url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "wukong returned empty download url",
                retryable=True,
            )
        return {
            "url": download_url,
            "name": str(info.get("name") or info.get("title") or f"{file_id}.bin"),
            "size": info.get("size"),
            "checksum": None,
            "headers": {"Referer": "https://pan.wkbrowser.com/"},
        }
