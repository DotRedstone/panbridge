"""天翼云盘 Adapter。

基于 cloud189（MIT License，https://github.com/s0urcelab/cloud189）
薄封装。cloud189 提供账号密码登录、文件列表、下载、上传等能力。

依赖：`cloud189`（PyPI，MIT License）
- `Cloud189({username, password})` 账号密码登录
- `get_all_files(folder_id)` 列出文件（扁平列表）
- `download(file_id)` 获取下载直链
- `get_disk_space_info()` 查询空间

字段映射（天翼云盘 -> 领域）：
- id -> file_id
- name -> name
- is_folder -> is_dir
- size -> size

能力边界（如实声明）：
- 账号文件列表、下载直链、登录校验：SDK 实际支持。
- 「分享解析 / 转存」：cloud189 SDK 不提供，天翼云盘分享接口需登录
  后的 sessionKey，协议未验证，如实标注为不支持。
- 登录方式为账号密码（无扫码），凭据加密存储。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from panbridge.domain.errors import DomainError, ErrorCode
from panbridge.domain.models import (
    Account,
    Share,
    ShareFile,
    TransferResult,
)
from panbridge.infrastructure.crypto import CredentialCipher
from panbridge.providers.base import ShareProvider

try:  # pragma: no cover - 依赖可选
    from cloud189 import Cloud189
except ImportError:  # pragma: no cover
    Cloud189 = None  # type: ignore


class Cloud189Provider(ShareProvider):
    name = "cloud189"
    display_name = "天翼云盘"
    capabilities = frozenset(
        {
            "list_account_files",
            "get_download_info",
            "validate_account",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        if Cloud189 is None:  # pragma: no cover
            raise RuntimeError(
                "cloud189 未安装，无法使用 Cloud189Provider。pip install cloud189"
            )
        self._cipher = cipher

    # ---- 内部工具 ----

    def _decrypt_credential(self, account: Account) -> dict[str, str]:
        """解密账号凭据，返回 {username, password} JSON。

        凭据以 JSON 字符串存储（含 username/password），加密后入库。
        """
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 account has no credential (username/password)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt",
                retryable=False,
            )
        try:
            plain = self._cipher.decrypt(token)
            data = json.loads(plain)
            if "username" not in data or "password" not in data:
                raise ValueError("missing username/password")
            return data
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 credential invalid (expect JSON with username/password)",
                retryable=False,
            ) from exc

    def _client_for(self, account: Account) -> Any:
        cred = self._decrypt_credential(account)
        return Cloud189(
            {"username": cred["username"], "password": cred["password"]}
        )

    async def _run(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            is_dir=bool(raw.get("is_folder", False)),
            size=raw.get("size"),
        )

    @staticmethod
    def _map_error(exc: Exception) -> DomainError:
        msg = str(exc)
        low = msg.lower()
        if "password" in low or "username" in low or "登录" in msg or "认证" in msg:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 credential invalid",
                retryable=False,
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"cloud189 api error: {msg[:200]}",
            retryable=True,
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "cloud189 share inspection not supported (needs login + unverified)",
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "cloud189 share listing not supported (needs login + unverified)",
        )

    async def validate_account(self, account: Account) -> bool:
        try:
            client = self._client_for(account)
            info = await self._run(client.get_disk_space_info)
            return isinstance(info, dict) and info.get("res_code") == 0
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
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "cloud189 share saving not supported (needs login + unverified)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        client = self._client_for(account)
        try:
            files = await self._run(
                client.get_all_files, parent_id or "-11"
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        return [self._map_file(f) for f in files if isinstance(f, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        client = self._client_for(account)
        try:
            url = await self._run(client.download, file_id)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "cloud189 returned empty download url",
                retryable=True,
            )
        return {
            "url": url,
            "name": f"{file_id}.bin",
            "size": None,
            "checksum": None,
            "headers": {},
        }
