"""UC 网盘 Adapter。

UC 网盘（drive.uc.cn）分享解析与转存需要登录态（Cookie）。协议参考
pan-relay（MIT 许可）的 `uc_client.py` 及多个公开逆向实现，代码为本项目
独立编写，不依赖第三方 SDK。

关键协议端点（`https://pc-api.uc.cn`，均带 `pr=UCBrowser&fr=pc`）：
1. 解析分享（需 Cookie，返回 stoken）：
   POST /1/clouddrive/share/sharepage/v2/detail
   body={passcode, pwd_id} → data.token_info.stoken
2. 分享文件列表：
   GET /1/clouddrive/share/sharepage/detail?pwd_id=&stoken=&pdir_fid=0
   → data.list[]（字段 fid / file_name / share_fid_token / file_type）
3. 转存：
   POST /1/clouddrive/share/sharepage/save
   body={fid_list, fid_token_list, to_pdir_fid, pwd_id, stoken, pdir_fid, scene}
   → data.task_id
4. 任务轮询：
   GET /1/clouddrive/task?task_id=&retry_index= → data.save_as.save_as_top_fids
5. 账号文件列表：
   GET /1/clouddrive/file/sort?pdir_fid=&_page=&_size=
6. 账号信息（校验登录态）：
   GET /1/clouddrive/member
7. 下载直链：
   POST /1/clouddrive/file/download body={fids:[fid]} → data[0].download_url

能力边界（如实声明）：
- 「解析分享 / 文件列表 / 转存 / 账号列表 / 下载直链」需 Cookie，协议已
  实现但未经真实账号验证。
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

_API = "https://pc-api.uc.cn"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://drive.uc.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_QUERY = {"pr": "UCBrowser", "fr": "pc"}


class UcProvider(ShareProvider):
    name = "uc"
    display_name = "UC 网盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "validate_account",
            "get_download_info",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        self._cipher = cipher

    # ---- 内部工具 ----

    def _cookie(self, account: Account) -> str:
        if account is None or not account.encrypted_credential:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "uc account has no credential (cookie)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt cookie",
            )
        try:
            return self._cipher.decrypt(account.encrypted_credential)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "uc credential could not be decrypted",
            ) from exc

    def _headers(self, account: Account) -> dict[str, str]:
        return {**_HEADERS, "cookie": self._cookie(account)}

    @staticmethod
    def _extract_pwd_id(share_id: str) -> str:
        # share_id 形如 /s/<pwd_id> 或完整 URL
        if "/" in share_id:
            import re

            m = re.search(r"/s/([A-Za-z0-9]+)", share_id)
            return m.group(1) if m else share_id
        return share_id

    @staticmethod
    def _map_error(data: dict[str, Any], ctx: str) -> DomainError:
        msg = str(data.get("message") or data.get("msg") or "")
        low = msg.lower()
        if "require login" in low or "guest" in low:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                f"uc {ctx}: cookie invalid or expired",
            )
        if "capacity limit" in low:
            return DomainError(
                ErrorCode.QUOTA_EXCEEDED,
                "uc quota exceeded",
            )
        if "密码" in msg or "提取码" in msg or "passcode" in low:
            return DomainError(
                ErrorCode.INVALID_PASSWORD,
                "uc share password incorrect",
            )
        if "失效" in msg or "不存在" in msg or "已删除" in msg:
            return DomainError(ErrorCode.INVALID_SHARE, "uc share invalid")
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"uc {ctx}: {msg or data.get('status')}",
            retryable=True,
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        # UC 网盘解析分享需要登录态（stoken），匿名不可用。
        # 因 inspect_share 签名无 account，返回需登录错误。
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "uc share inspection requires login; use an account-bound flow",
        )

    async def _fetch_share(
        self, account: Account, share_id: str, password: str | None
    ) -> tuple[str, list[ShareFile], dict[str, Any]]:
        """解析分享并返回 (stoken, files, detail)。"""
        pwd_id = self._extract_pwd_id(share_id)
        headers = self._headers(account)
        async with httpx.AsyncClient(timeout=30.0) as client:
            stoken_resp = await client.post(
                _API + "/1/clouddrive/share/sharepage/v2/detail",
                json={"passcode": password or "", "pwd_id": pwd_id},
                params=_QUERY,
                headers=headers,
            )
        if stoken_resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"uc share token request returned {stoken_resp.status_code}",
                retryable=True,
            )
        stoken_data = stoken_resp.json()
        if stoken_data.get("status") != 200:
            raise self._map_error(stoken_data, "share token")
        stoken = (
            (stoken_data.get("data") or {}).get("token_info") or {}
        ).get("stoken", "")
        if not stoken:
            raise DomainError(
                ErrorCode.INVALID_SHARE, "uc share missing stoken"
            )
        stoken = stoken.replace(" ", "+")

        async with httpx.AsyncClient(timeout=30.0) as client:
            detail_resp = await client.get(
                _API + "/1/clouddrive/share/sharepage/detail",
                params={
                    **_QUERY,
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "force": "0",
                    "_page": "1",
                    "_size": "100",
                    "_fetch_banner": "1",
                    "_fetch_share": "1",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                },
                headers=headers,
            )
        if detail_resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"uc share detail returned {detail_resp.status_code}",
                retryable=True,
            )
        detail_data = detail_resp.json()
        if detail_data.get("status") != 200:
            raise self._map_error(detail_data, "share detail")
        detail = detail_data.get("data") or {}
        files = [self._map_file(f) for f in detail.get("list") or []]
        return stoken, files, detail

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        # file_type: 0=目录, 1=文件（约定）
        ftype = raw.get("file_type")
        return ShareFile(
            file_id=str(raw.get("fid") or ""),
            name=raw.get("file_name") or raw.get("name") or "",
            is_dir=ftype == 0,
            size=raw.get("size"),
            parent_id=str(raw.get("pdir_fid") or ""),
        )

    async def list_share_files(
        self,
        share_id: str,
        password: str | None,
        parent_id: str | None = None,
        account: Account | None = None,
    ) -> list[ShareFile]:
        if account is None:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "uc share listing requires login (cookie)",
            )
        _, files, _ = await self._fetch_share(account, share_id, password)
        return files

    async def validate_account(self, account: Account) -> bool:
        headers = self._headers(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _API + "/1/clouddrive/member",
                    params=_QUERY,
                    headers=headers,
                )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return data.get("status") == 200
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
        stoken, files, _ = await self._fetch_share(account, share_id, password)
        selected = [f for f in files if f.file_id in set(file_ids)]
        if not selected:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "uc: no matching files selected for transfer",
            )
        fid_list = [f.file_id for f in selected]
        # fid_token 需从原始 detail 中取；此处重新获取 detail 的 list
        headers = self._headers(account)
        pwd_id = self._extract_pwd_id(share_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            detail_resp = await client.get(
                _API + "/1/clouddrive/share/sharepage/detail",
                params={
                    **_QUERY,
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "_size": "100",
                },
                headers=headers,
            )
        detail = (
            (detail_resp.json().get("data") or {})
            if detail_resp.status_code == 200
            else {}
        )
        token_map = {
            str(f.get("fid")): f.get("share_fid_token")
            for f in detail.get("list") or []
        }
        fid_token_list = [token_map.get(fid, "") for fid in fid_list]

        async with httpx.AsyncClient(timeout=30.0) as client:
            save_resp = await client.post(
                _API + "/1/clouddrive/share/sharepage/save",
                json={
                    "fid_list": fid_list,
                    "fid_token_list": fid_token_list,
                    "to_pdir_fid": destination or "0",
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "scene": "link",
                },
                params={"entry": "update_share", **_QUERY},
                headers=headers,
            )
        if save_resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"uc save returned {save_resp.status_code}",
                retryable=True,
            )
        save_data = save_resp.json()
        if save_data.get("status") != 200:
            raise self._map_error(save_data, "save")
        task_id = (save_data.get("data") or {}).get("task_id")
        if not task_id:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "uc save missing task_id", retryable=True
            )

        saved = await self._wait_task(account, task_id)
        return TransferResult(
            saved_file_ids=saved,
            remote_task_id=str(task_id),
        )

    async def _wait_task(
        self, account: Account, task_id: str, retries: int = 50
    ) -> list[str]:
        headers = self._headers(account)
        for idx in range(retries):
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _API + "/1/clouddrive/task",
                    params={**_QUERY, "task_id": task_id, "retry_index": idx},
                    headers=headers,
                )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("status") != 200:
                msg = str(data.get("message") or "")
                if "capacity limit" in msg.lower():
                    raise DomainError(
                        ErrorCode.QUOTA_EXCEEDED, "uc quota exceeded"
                    )
                continue
            task = data.get("data") or {}
            save_as_fids = (
                (task.get("save_as") or {}).get("save_as_top_fids") or []
            )
            if task.get("status") == 2 or save_as_fids:
                return [str(f) for f in save_as_fids]
            if task.get("status") == 3:
                raise DomainError(
                    ErrorCode.INTERNAL_ERROR,
                    f"uc task failed: {task.get('message') or 'unknown'}",
                )
            import asyncio

            await asyncio.sleep(0.5)
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            "uc task polling timed out",
            retryable=True,
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        headers = self._headers(account)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _API + "/1/clouddrive/file/sort",
                params={
                    **_QUERY,
                    "pdir_fid": parent_id or "0",
                    "_page": "1",
                    "_size": "100",
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                },
                headers=headers,
            )
        if resp.status_code != 200:
            raise self._map_error({}, "file list")
        data = resp.json()
        if data.get("status") != 200:
            raise self._map_error(data, "file list")
        return [self._map_file(f) for f in (data.get("data") or {}).get("list") or []]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        headers = self._headers(account)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _API + "/1/clouddrive/file/download",
                json={"fids": [file_id]},
                params=_QUERY,
                headers=headers,
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"uc download returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        if data.get("status") != 200:
            raise self._map_error(data, "download")
        items = data.get("data") or []
        if not items:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED, "uc returned empty download info"
            )
        url = items[0].get("download_url") or items[0].get("url")
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "uc returned empty download url",
                retryable=True,
            )
        return {
            "url": url,
            "name": items[0].get("file_name") or items[0].get("name"),
            "size": items[0].get("size"),
            "checksum": None,
            "headers": {
                "Cookie": self._cookie(account),
                "Referer": "https://drive.uc.cn/",
                "User-Agent": _HEADERS["User-Agent"],
            },
        }
