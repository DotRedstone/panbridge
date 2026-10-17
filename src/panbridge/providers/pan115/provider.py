"""115 网盘 Adapter。

基于 p115client（MIT License，https://github.com/ChenyangGao/p115client）
薄封装。p115client 提供同步/异步 API，全面封装 web/app/open 接口。

依赖：`p115client`（PyPI，MIT License）
- `share_snap` 解析分享（**匿名可用**，无需登录）
- `share_receive` 转存分享文件（需登录）
- `download_url_app` 获取下载直链（需登录）
- `fs_files` 列出账号文件（需登录）
- `fs_mkdir` 创建目录（需登录）

字段映射（115 -> 领域）：
- fid -> file_id
- n -> name
- pid (目录判断) -> is_dir
- s -> size

能力边界（如实声明）：
- 「解析分享 + 文件列表」：匿名可用（share_snap 无需登录）。
- 「转存 / 下载直链 / 账号文件列表」：需登录 Cookie。
"""

from __future__ import annotations

import asyncio
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
    from p115client import P115Client
    from p115client import check_response as _check_response
except ImportError:  # pragma: no cover
    P115Client = None  # type: ignore
    _check_response = None  # type: ignore


class Pan115Provider(ShareProvider):
    name = "115"
    display_name = "115 网盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "get_download_info",
            "validate_account",
            "create_folder",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        if P115Client is None:  # pragma: no cover
            raise RuntimeError(
                "p115client 未安装，无法使用 Pan115Provider。pip install p115client"
            )
        self._cipher = cipher

    # ---- 内部工具 ----

    def _decrypt_credential(self, account: Account) -> str:
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "115 account has no credential (cookie)",
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
                "115 credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

    def _client_for(self, account: Account) -> Any:
        cookie = self._decrypt_credential(account)
        return P115Client(cookie)

    async def _run(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        # 115 目录：有 pid 字段（文件无 pid，或 pid 表示父目录）。
        # 实际区分：目录有 "pid" 键且 cid 前缀不同。这里用 fid 存在性 +
        # 常见字段判断。
        fid = str(raw.get("fid") or raw.get("file_id") or "")
        return ShareFile(
            file_id=fid,
            name=raw.get("n") or raw.get("name") or "",
            is_dir=bool(raw.get("pid") is not None),
            size=raw.get("s") or raw.get("size"),
            parent_id=str(raw.get("pid") or ""),
        )

    @staticmethod
    def _map_error(exc: Exception) -> DomainError:
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "登录" in msg or "未登录" in msg or "cookie" in low:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "115 account credential is invalid or expired",
                retryable=False,
            )
        if "404" in msg or "不存在" in msg or "失效" in msg:
            return DomainError(ErrorCode.INVALID_SHARE, "invalid 115 share")
        if "密码" in msg or "提取码" in msg or "passcode" in low \
                or "receive_code" in low:
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "115 share password incorrect"
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"115 api error: {msg[:200]}",
            retryable=True,
        )

    async def _snap(
        self, share_id: str, password: str | None, cid: str = "0"
    ) -> dict[str, Any]:
        """解析分享目录（匿名），返回 share_snap 响应。"""
        client = P115Client("")  # 空 cookie，匿名
        try:
            result = await self._run(
                client.share_snap,
                {
                    "share_code": share_id,
                    "receive_code": password or "",
                    "cid": cid,
                    "limit": 1150,
                },
            )
            return _check_response(result) if _check_response else result
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        data = await self._snap(share_id, password)
        files_raw = data.get("data", []) if isinstance(data, dict) else []
        files = [self._map_file(f) for f in files_raw if isinstance(f, dict)]
        title = f"115-share-{share_id}"
        return Share(
            provider=self.name,
            share_id=share_id,
            title=title,
            files=files,
            password=password,
            raw=data if isinstance(data, dict) else {},
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        data = await self._snap(share_id, password, parent_id or "0")
        files_raw = data.get("data", []) if isinstance(data, dict) else []
        return [self._map_file(f) for f in files_raw if isinstance(f, dict)]

    async def validate_account(self, account: Account) -> bool:
        try:
            client = self._client_for(account)
            info = await self._run(client.fs_files, {"limit": 1})
            return isinstance(info, dict)
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
        client = self._client_for(account)
        try:
            result = await self._run(
                client.share_receive,
                {
                    "share_code": share_id,
                    "receive_code": password or "",
                    "file_id": ",".join(file_ids),
                    "cid": destination or "0",
                },
            )
            _check_response(result)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        return TransferResult(saved_file_ids=[])

    async def resolve_saved_file_ids(
        self, account: Account, destination: str | None, names: list[str]
    ) -> list[str]:
        """转存后从目标目录按文件名解析新 fid。

        115 的 share_receive 转存是异步的，文件落在 destination(cid) 目录，
        获得新的 pick_code(fid)。下载必须用新 fid，故按文件名匹配。
        同名文件匹配到多个时抛出错误，避免静默下错文件。
        """
        files = await self.list_account_files(account, destination or "0")
        name_set = set(names)
        by_name: dict[str, list[str]] = {}
        for item in files:
            if item.name in name_set and not item.is_dir:
                by_name.setdefault(item.name, []).append(item.file_id)

        resolved: list[str] = []
        for name in names:
            candidates = by_name.get(name, [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"ambiguous file name after save: {name!r} matched "
                    f"{len(candidates)} files; use a dedicated empty folder",
                    retryable=False,
                )
        return resolved

    async def create_folder(
        self, account: Account, name: str, parent_id: str = "0"
    ) -> str:
        client = self._client_for(account)
        try:
            result = await self._run(
                client.fs_mkdir, {"cname": name, "pid": parent_id}
            )
            _check_response(result)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        fid = result.get("data", {}).get("cid") if isinstance(result, dict) else None
        if not fid:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "115 create_folder returned no cid",
                retryable=False,
            )
        return str(fid)

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        client = self._client_for(account)
        try:
            result = await self._run(
                client.fs_files,
                {"cid": parent_id or "0", "limit": 1150},
            )
            _check_response(result)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        items = result.get("data", []) if isinstance(result, dict) else []
        return [self._map_file(it) for it in items if isinstance(it, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        client = self._client_for(account)
        try:
            result = await self._run(
                client.download_url_app, {"pick_code": file_id}
            )
            _check_response(result)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        url = ""
        name = f"{file_id}.bin"
        if isinstance(result, dict):
            data = result.get("data", {})
            if isinstance(data, dict) and data:
                for k, v in data.items():
                    if isinstance(v, dict) and v.get("url", {}).get("url"):
                        name = k
                        url = v["url"]["url"]
                        break
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "115 returned empty download url",
                retryable=True,
            )
        return {
            "url": url,
            "name": name,
            "size": None,
            "checksum": None,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                    "Mobile Safari/537.36"
                )
            },
        }
