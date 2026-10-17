"""阿里云盘真实 Adapter。

复用 aligo（MIT License）定义的 API 端点常量与请求/响应 schema，
但**用独立 httpx 客户端发起匿名请求**，以隔离 aligo 的登录副作用
（aligo 的 `Aligo()` / `Auth()` 会阻塞轮询二维码、打印终端 ASCII、
写 `~/.aligo/` 配置目录）。

依赖：`aligo`（PyPI，MIT License） https://github.com/foyoux/aligo

关键事实（已用真实公开分享验证）：
- `get_share_token`（POST /v2/share_link/get_share_token）→ 匿名，无需登录。
- `list_by_share`（POST /adrive/v2/file/list_by_share）→ 匿名，`x-share-token` 头。
- `get_share_by_anonymous`（POST /adrive/v2/share_link/get_share_by_anonymous）
  → 匿名，返回 share_name / file_count / file_infos。
- 分享直链下载 `get_share_link_download_url` → 匿名。
- 「转存」`share_file_saveto_drive` → 需要登录（refresh_token），
  该路径仍复用 aligo 的 `Aligo(refresh_token=...)`（登录副作用通过
  `show` 回调静默、`level` 抑制日志）。

因此「解析分享 / 预览 / 直链下载」无需账号；只有「转存」需要登录。

字段映射（阿里云盘 -> 领域）：
- file_id -> file_id
- name -> name
- type ('file'/'folder') -> is_dir
- size -> size
"""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖可选
    from aligo import Aligo
except ImportError:  # pragma: no cover
    Aligo = None  # type: ignore

_API_HOST = "https://api.aliyundrive.com"
_HEADERS = {
    "Referer": "https://aliyundrive.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


class AliyunProvider(ShareProvider):
    name = "aliyun"
    display_name = "阿里云盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "get_download_info",
            "validate_account",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        if Aligo is None:  # pragma: no cover
            raise RuntimeError(
                "aligo 未安装，无法使用 AliyunProvider。pip install aligo"
            )
        self._cipher = cipher

    # ---- 内部工具 ----

    async def _get_share_token(self, share_id: str, password: str | None) -> str:
        """匿名获取分享 token（无需登录）。"""
        payload = {"share_id": share_id, "share_pwd": password or ""}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _API_HOST + "/v2/share_link/get_share_token",
                headers=_HEADERS,
                json=payload,
            )
        if resp.status_code != 200:
            raise self._map_status(resp)
        data = resp.json()
        token = data.get("share_token")
        if not token:
            msg = data.get("message") or data.get("code") or "no share_token"
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"aliyun share token failed: {msg}",
            )
        return token

    async def _anon_post(
        self, path: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """匿名 POST，附带 x-share-token 头。"""
        headers = {**_HEADERS, "x-share-token": token}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _API_HOST + path, headers=headers, json=body
            )
        if resp.status_code != 200:
            raise self._map_status(resp)
        return resp.json()

    @staticmethod
    def _map_status(resp: httpx.Response) -> DomainError:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        code = str(data.get("code", "")).lower()
        msg = str(data.get("message", ""))
        if (
            "cancelled" in msg
            or "not found" in msg
            or "invalid" in msg
            or "失效" in msg
        ):
            return DomainError(ErrorCode.INVALID_SHARE, "aliyun share invalid")
        if "password" in msg.lower() or "提取码" in msg or "pwd" in msg.lower():
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "aliyun share password incorrect"
            )
        if resp.status_code == 401 or "token" in code:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "aliyun token invalid or expired",
                retryable=False,
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"aliyun api error: {msg or code or resp.status_code}",
            retryable=True,
        )

    def _client(self, account: Account) -> Any:
        """用账号的 refresh_token 构建 aligo 客户端（仅转存需要登录）。

        副作用隔离：`show=lambda _: None` 静默二维码、`level=logging.ERROR`
        抑制终端日志、`re_login=False` 避免 token 失效时阻塞重新登录。
        """
        if Aligo is None:  # pragma: no cover
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "aligo not installed",
                retryable=False,
            )
        refresh_token = self._decrypt_credential(account)
        return Aligo(
            name=f"panbridge-{account.id}",
            refresh_token=refresh_token,
            show=lambda _: None,
            level=logging.ERROR,
            re_login=False,
        )

    def _decrypt_credential(self, account: Account) -> str:
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "aliyun account has no credential (refresh_token)",
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
                "aliyun credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

    async def _run(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _map_share_item(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("file_id", "")),
            name=raw.get("name", ""),
            is_dir=bool(raw.get("type") == "folder"),
            size=raw.get("size"),
            parent_id=raw.get("parent_file_id"),
        )

    @staticmethod
    def _map_file(raw: Any) -> ShareFile:
        return ShareFile(
            file_id=str(getattr(raw, "file_id", "")),
            name=getattr(raw, "name", ""),
            is_dir=bool(getattr(raw, "type", "") == "folder"),
            size=getattr(raw, "size", None),
            parent_id=getattr(raw, "parent_file_id", None),
        )

    @staticmethod
    def _map_error(exc: Exception) -> DomainError:
        msg = str(exc)
        low = msg.lower()
        if "token" in low and (
            "invalid" in low or "expired" in low or "refresh" in low
        ):
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "aliyun account token invalid or expired",
                retryable=False,
            )
        if "share" in low and (
            "cancelled" in low or "not found" in low or "invalid" in low
        ):
            return DomainError(ErrorCode.INVALID_SHARE, "invalid aliyun share")
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"aliyun api error: {msg[:200]}",
            retryable=True,
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        token = await self._get_share_token(share_id, password)

        # 分享详情（标题）与文件列表
        detail = await self._anon_post(
            "/adrive/v2/share_link/get_share_by_anonymous",
            token,
            {"share_id": share_id},
        )
        listing = await self._anon_post(
            "/adrive/v2/file/list_by_share",
            token,
            {
                "share_id": share_id,
                "share_token": token,
                "parent_file_id": "root",
                "limit": 200,
            },
        )

        items = listing.get("items", [])
        title = detail.get("share_name") or detail.get("share_title") or (
            f"aliyun-share-{share_id}"
        )
        return Share(
            provider=self.name,
            share_id=share_id,
            title=title,
            files=[self._map_share_item(it) for it in items],
            password=password,
            raw={"token": token},
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        token = await self._get_share_token(share_id, password)
        listing = await self._anon_post(
            "/adrive/v2/file/list_by_share",
            token,
            {
                "share_id": share_id,
                "share_token": token,
                "parent_file_id": parent_id or "root",
                "limit": 200,
            },
        )
        return [self._map_share_item(it) for it in listing.get("items", [])]

    async def validate_account(self, account: Account) -> bool:
        try:
            client = self._client(account)
            info = await self._run(client.get_personal_info)
            return info is not None
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
        client = self._client(account)
        try:
            token = await self._run(
                client.get_share_token, share_id, password or ""
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        saved_file_ids: list[str] = []
        failed: list[dict[str, Any]] = []
        for fid in file_ids:
            try:
                resp = await self._run(
                    client.share_file_saveto_drive,
                    fid,
                    token,
                    destination or "root",
                )
                new_id = getattr(resp, "file_id", None)
                saved_file_ids.append(str(new_id) if new_id else fid)
            except Exception as exc:  # noqa: BLE001
                failed.append({"file_id": fid, "error": str(exc)[:200]})

        return TransferResult(
            saved_file_ids=saved_file_ids,
            failed=failed,
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        client = self._client(account)
        try:
            files = await self._run(client.get_file_list, parent_id or "root")
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        return [self._map_file(f) for f in files]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        """获取文件下载直链（需登录，转存后文件）。

        返回下载 URL 与文件名。阿里云盘直链已带签名，无需额外 Cookie。
        """
        client = self._client(account)
        try:
            resp = await self._run(client.get_download_url, file_id)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        url = getattr(resp, "url", None)
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "aliyun returned empty download url",
                retryable=True,
            )
        return {
            "url": url,
            "name": getattr(resp, "name", None) or f"{file_id}.bin",
            "size": getattr(resp, "size", None),
            "checksum": getattr(resp, "crc64_hash", None),
            "headers": {},
        }
