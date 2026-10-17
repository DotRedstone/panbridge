"""天翼云盘 Adapter。

天翼云盘（cloud.189.cn）同时支持「账号操作」与「分享解析/转存」两类能力，
分别使用两种数据源：

1. 账号文件列表 / 账号下载直链：基于 cloud189（MIT License，
   https://github.com/s0urcelab/cloud189）薄封装（账号密码登录）。
2. 分享解析 / 转存 / 分享下载直链：基于公开 HTTP 协议自行实现（参考
   cloud189-auto-save（MIT）的 `cloud189.js`，代码为本项目独立编写）。

分享协议端点（`https://cloud.189.cn`，JSON 响应）：
1. 解析分享（匿名可用，返回 shareId/shareMode/fileCount/accessCode 等）：
   GET /api/open/share/getShareInfoByCodeV2.action?shareCode=<code>
   → {res_code, shareId, shareMode, fileCount, fileName, accessCode, ...}
2. 校验访问码（匿名，返回 sessionKey 用于后续列表）：
   GET /api/open/share/checkAccessCode.action?shareCode=&accessCode=&uuid=
3. 分享文件列表（匿名，需访问码校验后）：
   GET /api/open/share/listShareDir.action?shareId=&fileId=&shareMode=
       &accessCode=&isFolder=&pageNum=1&pageSize=1000
   → {fileListAO: {fileList: [{id, name, size, isFolder, md5}]}}
4. 转存（需登录，SSON Cookie）：
   POST /api/open/batch/createBatchTask.action (form)
   {type:"SHARE_SAVE", taskInfos:[{fileId,fileName,isFolder}],
    targetFolderId, shareId} → {res_code:0, taskId}
5. 轮询转存状态：
   POST /api/open/batch/checkBatchTask.action (form) {taskId, type:"SHARE_SAVE"}
6. 分享下载直链（需登录）：
   GET /api/portal/getNewVlcVideoPlayUrl.action?fileId=&shareId=&type=4&dt=1
   → {normal:{code:1,url}}，再跟随 302 取 location

能力边界（如实声明）：
- 「解析分享 / 文件列表」匿名可用（需访问码时先 checkAccessCode）。
- 「转存 / 下载直链」需登录（SSON Cookie），协议已实现但未经真实账号验证。
- 账号文件列表/下载沿用 cloud189 SDK（账号密码登录）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urlparse

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

try:  # pragma: no cover - 依赖可选
    from cloud189 import Cloud189
except ImportError:  # pragma: no cover
    Cloud189 = None  # type: ignore

_API = "https://cloud.189.cn"

_HEADERS = {
    "Accept": "application/json;charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _extract_share_code(share_id: str) -> str:
    """从 URL 或纯 code 中提取天翼云盘分享码。

    支持 https://cloud.189.cn/t/<code> 或纯 <code>。
    """
    if "189.cn" in share_id:
        path = urlparse(share_id).path
        parts = [p for p in path.split("/") if p]
        if parts:
            # /t/<code> 或 /web/share?code=<code>
            return parts[-1]
        # code 作为 query 参数（/web/share?code=xxx）
        query = urlparse(share_id).query
        if "code=" in query:
            for pair in query.split("&"):
                if pair.startswith("code="):
                    return pair.split("=", 1)[1]
    return share_id


class Cloud189Provider(ShareProvider):
    name = "cloud189"
    display_name = "天翼云盘"
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
        if Cloud189 is None:  # pragma: no cover
            raise RuntimeError(
                "cloud189 未安装，无法使用 Cloud189Provider。pip install cloud189"
            )
        self._cipher = cipher

    # ---- 凭据 ---- #

    def _decrypt_credential(self, account: Account) -> dict[str, str]:
        """解密账号凭据，返回 dict。

        支持两种形式（均为加密 JSON 字符串）：
        - {username, password}：账号密码登录（cloud189 SDK 用）
        - {cookie}：SSON Cookie（分享转存/下载用）
        """
        token = account.encrypted_credential or ""
        if not token:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 account has no credential",
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
            if not isinstance(data, dict):
                raise ValueError("credential not a JSON object")
            return data
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 credential invalid (expect JSON object)",
                retryable=False,
            ) from exc

    def _client_for(self, account: Account) -> Any:
        cred = self._decrypt_credential(account)
        if "username" not in cred or "password" not in cred:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 account missing username/password for SDK",
            )
        return Cloud189(
            {"username": cred["username"], "password": cred["password"]}
        )

    def _cookie_for(self, account: Account) -> str:
        cred = self._decrypt_credential(account)
        cookie = cred.get("cookie") or cred.get("Cookie") or ""
        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "cloud189 account has no SSON cookie for share transfer",
            )
        return cookie

    async def _run(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    # ---- 分享协议（HTTP） ---- #

    async def _share_get(
        self, path: str, params: dict[str, Any], *, cookie: str | None = None
    ) -> dict[str, Any]:
        headers = dict(_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        url = f"{_API}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"cloud189 {path} returned {resp.status_code}",
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INVALID_SHARE, "cloud189 share returned non-JSON"
            ) from exc

    async def _share_post(
        self,
        path: str,
        data: dict[str, Any],
        *,
        cookie: str | None = None,
    ) -> dict[str, Any]:
        headers = dict(_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        url = f"{_API}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, data=data, headers=headers)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"cloud189 {path} returned {resp.status_code}",
                retryable=True,
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "cloud189 returned non-JSON"
            ) from exc

    async def _get_share_info(
        self, share_code: str, access_code: str | None
    ) -> dict[str, Any]:
        """解析分享元信息，返回 JSON（含 shareId/shareMode/accessCode 等）。"""
        if access_code:
            param = f"{share_code}（访问码：{access_code}）"
        else:
            param = share_code
        data = await self._share_get(
            "/api/open/share/getShareInfoByCodeV2.action",
            {"shareCode": param, "noCache": str(uuid.uuid4())},
        )
        if data.get("res_code") not in (0, "0", None, "ShareAuditWaiting"):
            self._raise_share_error(data)
        return data

    @staticmethod
    def _raise_share_error(data: dict[str, Any]) -> None:
        res_code = str(data.get("res_code") or "")
        msg = str(data.get("res_message") or data.get("res_msg") or "")
        low = f"{res_code} {msg}".lower()
        access_keys = [
            "accesscode",
            "access code",
            "needaccesscode",
            "访问码",
            "提取码",
            "密码",
        ]
        if any(k in low for k in access_keys):
            raise DomainError(
                ErrorCode.INVALID_PASSWORD,
                "cloud189 share access code incorrect",
            )
        invalid_keys = [
            "notfound",
            "not found",
            "expired",
            "不存在",
            "失效",
            "过期",
            "取消",
        ]
        if any(k in low for k in invalid_keys):
            raise DomainError(ErrorCode.INVALID_SHARE, "cloud189 share invalid")
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"cloud189 api error: {res_code} {msg}",
            retryable=True,
        )

    async def _list_share_dir(
        self,
        share_id: str,
        file_id: str,
        share_mode: int,
        access_code: str | None,
        is_folder: bool = True,
    ) -> list[dict[str, Any]]:
        data = await self._share_get(
            "/api/open/share/listShareDir.action",
            {
                "shareId": share_id,
                "isFolder": "true" if is_folder else "false",
                "fileId": file_id,
                "orderBy": "lastOpTime",
                "descending": "true",
                "shareMode": share_mode,
                "pageNum": 1,
                "pageSize": 1000,
                "accessCode": access_code or "",
            },
        )
        if data.get("res_code") not in (0, "0", None):
            self._raise_share_error(data)
        file_list = data.get("fileListAO", {}).get("fileList", [])
        return [f for f in file_list if isinstance(f, dict)]

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            is_dir=bool(raw.get("isFolder") or raw.get("is_folder")),
            size=raw.get("size"),
            parent_id=str(raw.get("parentId") or ""),
        )

    # ---- Provider 接口 ---- #

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        code = _extract_share_code(share_id)
        info = await self._get_share_info(code, password)
        share_uid = str(info.get("shareId") or "")
        share_mode = int(info.get("shareMode") or 1)
        access_code = info.get("accessCode") or password or ""
        file_id = str(info.get("fileId") or "0")
        files: list[ShareFile] = []
        try:
            raw_files = await self._list_share_dir(
                share_uid, file_id, share_mode, access_code
            )
            files = [self._map_file(f) for f in raw_files]
        except DomainError:
            # 列表失败（如目录）时，至少返回分享标题元信息
            files = []
        title = str(info.get("shareName") or info.get("fileName") or f"189-{code}")
        return Share(
            provider=self.name,
            share_id=share_uid or code,
            title=title,
            files=files,
            password=password,
            raw=info,
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        code = _extract_share_code(share_id)
        info = await self._get_share_info(code, password)
        share_uid = str(info.get("shareId") or "")
        share_mode = int(info.get("shareMode") or 1)
        access_code = info.get("accessCode") or password or ""
        file_id = parent_id or str(info.get("fileId") or "0")
        raw_files = await self._list_share_dir(
            share_uid, file_id, share_mode, access_code
        )
        return [self._map_file(f) for f in raw_files]

    async def validate_account(self, account: Account) -> bool:
        cred = self._decrypt_credential(account)
        # 优先用 cookie 校验（分享转存场景），否则回退 SDK 账号密码校验
        if cred.get("cookie"):
            try:
                data = await self._share_get(
                    "/api/open/file/getUserInfo.action", {}, cookie=cred["cookie"]
                )
                return isinstance(data, dict)
            except DomainError:
                return False
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
        code = _extract_share_code(share_id)
        cookie = self._cookie_for(account)
        info = await self._get_share_info(code, password)
        share_uid = str(info.get("shareId") or code)
        # 构造转存任务
        task_infos = [
            {"fileId": fid, "fileName": "", "isFolder": 0} for fid in file_ids
        ]
        result = await self._share_post(
            "/api/open/batch/createBatchTask.action",
            {
                "type": "SHARE_SAVE",
                "taskInfos": json.dumps(task_infos),
                "targetFolderId": destination or "-11",
                "shareId": share_uid,
            },
            cookie=cookie,
        )
        if result.get("res_code") not in (0, "0", None):
            self._raise_share_error(result)
        task_id = str(result.get("taskId") or "")
        return TransferResult(
            saved_file_ids=list(file_ids), remote_task_id=task_id
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        client = self._client_for(account)
        try:
            files = await self._run(client.get_all_files, parent_id or "-11")
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        return [self._map_file(f) for f in files if isinstance(f, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        # 优先用 SDK 账号下载直链
        try:
            client = self._client_for(account)
            url = await self._run(client.download, file_id)
            if url:
                return {
                    "url": url,
                    "name": f"{file_id}.bin",
                    "size": None,
                    "checksum": None,
                    "headers": {},
                }
        except DomainError:
            raise
        except Exception:  # noqa: BLE001
            pass
        # 回退：分享下载直链（需 cookie + shareId）
        raise DomainError(
            ErrorCode.DOWNLOAD_FAILED,
            "cloud189 download requires a valid account credential",
            retryable=True,
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
