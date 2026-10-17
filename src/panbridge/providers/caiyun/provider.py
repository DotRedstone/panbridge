"""中国移动云盘（和彩云）Adapter。

移动云盘分享解析与转存走独立加密通道（share-kd-njs.yun.139.com），协议
参考 pan-relay（MIT 许可）的 `caiyun_client.py` 与 `caiyun_protocol.py`，
代码为本项目独立编写，不依赖第三方 SDK。

登录态：`Authorization: Basic <token>`（token 为 Basic 后的内容，可从
浏览器 DevTools 的 `yun.139.com` 请求头获取）。

关键协议端点：
1. 分享解析（需 Authorization）：
   POST share-kd-njs.yun.139.com/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6
   body(AES加密)={getOutLinkInfoReq:{account, linkID, passwd, ...}}
   → data.caLst[]（目录）/ coLst[]（文件）
2. 转存：POST .../IBatchOprTask/createOuterLinkBatchOprTask → taskID
3. 任务轮询：POST .../IBatchOprTask/queryBatchOprTaskDetail
4. 账号文件列表（个人云，需 mcloud-sign 签名）：
   POST personal-kd-njs.yun.139.com/hcy/file/list

能力边界（如实声明）：
- 「分享解析 / 转存」：需 Authorization，协议已实现但未经真实账号验证。
- 「账号文件列表 / 下载」：需 Authorization，端点已实现但未经真实账号
  验证（端点 URL 可能随版本变化）。
"""

from __future__ import annotations

import base64
import re
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
from panbridge.providers.caiyun.protocol import (
    PERSONAL_API_BASE,
    SHARE_API_BASE,
    TASK_PATH,
    TRANSFER_PATH,
    decode_share_response,
    encode_share_payload,
    mcloud_sign,
)

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://yun.139.com",
    "Referer": "https://yun.139.com/shareweb/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
}


class CaiyunProvider(ShareProvider):
    name = "caiyun"
    display_name = "中国移动云盘"
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
            )
        try:
            return self._cipher.decrypt(token)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "caiyun credential could not be decrypted",
            ) from exc

    def _authorization(self, account: Account) -> str:
        token = re.sub(r"^Basic\s+", "", self._decrypt_credential(account), flags=re.I)
        return f"Basic {token}" if token else ""

    def _account_phone(self, account: Account) -> str:
        token = re.sub(r"^Basic\s+", "", self._decrypt_credential(account), flags=re.I)
        try:
            return base64.b64decode(token).decode("utf-8").split(":")[1]
        except (IndexError, UnicodeError, ValueError):  # noqa: BLE001
            return ""

    def _auth_headers(self, account: Account) -> dict[str, str]:
        return {**_HEADERS, "Authorization": self._authorization(account)}

    @staticmethod
    def _parse_share_id(share_id: str) -> str:
        if "/" in share_id or "139.com" in share_id:
            m = re.search(r"/w/i/([A-Za-z0-9_-]+)", share_id)
            if m:
                return m.group(1)
        return share_id

    @staticmethod
    def _ok(result: dict[str, Any] | None) -> bool:
        if not result:
            return False
        code = str(result.get("code", result.get("resultCode", "")))
        if code in ("0", "0000"):
            return result.get("success") is not False
        if "code" not in result and "resultCode" not in result and result.get(
            "success"
        ) is True:
            return True
        return False

    async def _request_share_api(
        self, account: Account, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {
            **_HEADERS,
            "X-Yun-Api-Version": "v1",
            "X-Deviceinfo": "||9|12.27.0|chrome|139.0.0.0|||windows 10||zh-CN|||",
            "hcy-cool-flag": "1",
            "CMS-DEVICE": "default",
            "x-m4c-caller": "PC",
        }
        if self._authorization(account):
            headers["Authorization"] = self._authorization(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    SHARE_API_BASE + path,
                    content=encode_share_payload(payload),
                    headers=headers,
                )
            resp.raise_for_status()
            result = decode_share_response(resp.content)
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun share api error: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        if not self._ok(result):
            code = result.get("code")
            msg = str(result.get("desc") or result.get("message") or "")
            if "密码" in msg or "passwd" in msg.lower() or code in ("2001", "2002"):
                raise DomainError(
                    ErrorCode.INVALID_PASSWORD, "caiyun share password incorrect"
                )
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun share api failed: {msg or code}",
                retryable=True,
            )
        return result

    async def _request_signed_api(
        self, account: Account, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._authorization(account):
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED, "caiyun missing authorization token"
            )
        headers = {
            "Authorization": self._authorization(account),
            "CMS-DEVICE": "default",
            "Caller": "web",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-route": "001",
            "mcloud-sign": mcloud_sign(payload),
            "mcloud-version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            result = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun signed api error: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        if not self._ok(result):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"caiyun signed api failed: {result.get('code')} "
                f"{result.get('message')}",
                retryable=True,
            )
        return result

    async def _get_share_entries(
        self, account: Account, share_id: str, password: str | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        sid = self._parse_share_id(share_id)
        phone = self._account_phone(account)
        entries: list[dict[str, Any]] = []
        info: dict[str, Any] = {}
        for start in range(1, 10002, 200):
            result = await self._request_share_api(
                account,
                "/richlifeApp/devapp/IOutLink/getOutLinkInfoV6",
                {
                    "getOutLinkInfoReq": {
                        "account": phone,
                        "linkID": sid,
                        "passwd": password or "",
                        "caSrt": 0,
                        "coSrt": 0,
                        "srtDr": 1,
                        "bNum": start,
                        "pCaID": "root",
                        "eNum": start + 199,
                    }
                },
            )
            data = result.get("data")
            if not isinstance(data, dict):
                return {}, []
            if not info:
                info = data
            page = (data.get("caLst") or []) + (data.get("coLst") or [])
            entries.extend(page)
            if len(page) < 200:
                break
        return info, entries

    @staticmethod
    def _map_share_entry(raw: dict[str, Any]) -> ShareFile:
        # 目录用 caID/caName，文件用 coID/coName
        is_dir = bool(raw.get("caID"))
        return ShareFile(
            file_id=str(raw.get("coID") or raw.get("caID") or ""),
            name=raw.get("coName") or raw.get("caName") or "",
            is_dir=is_dir,
            size=raw.get("coSize"),
            parent_id=str(raw.get("pCaID") or "root"),
        )

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("fileId") or raw.get("fid") or ""),
            name=raw.get("name") or raw.get("fileName") or "",
            is_dir=raw.get("type") == "folder" or raw.get("isDir") is True,
            size=raw.get("size") or raw.get("fileSize"),
            parent_id=str(raw.get("parentFileId") or ""),
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        # 移动云盘解析分享需要 Authorization（登录态），匿名不可用。
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "caiyun share inspection requires login; use an account-bound flow",
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
                "caiyun share listing requires login (authorization)",
            )
        _, entries = await self._get_share_entries(account, share_id, password)
        return [self._map_share_entry(e) for e in entries]

    async def validate_account(self, account: Account) -> bool:
        headers = self._auth_headers(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://yun.139.com/api/user/info", headers=headers
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
        sid = self._parse_share_id(share_id)
        phone = self._account_phone(account)
        info, entries = await self._get_share_entries(account, sid, password)
        if not entries:
            raise DomainError(ErrorCode.INVALID_SHARE, "caiyun share empty")

        selected = [
            e
            for e in entries
            if str(e.get("coID") or e.get("caID")) in set(file_ids)
        ]
        if not selected:
            # 未指定 file_ids 时转存全部
            selected = entries

        catalogs = [x.get("path") for x in selected if x.get("caID")]
        contents = [x.get("path") for x in selected if x.get("coID")]
        task_info = {
            "linkID": sid,
            "contentInfoList": contents,
            "catalogInfoList": catalogs,
            "newCatalogID": destination or "root",
            "newCatalogName": "全部",
            "needPassword": bool(password),
        }
        result = await self._request_share_api(
            account,
            TRANSFER_PATH,
            {
                "createOuterLinkBatchOprTaskReq": {
                    "msisdn": phone,
                    "ownerAccount": info.get("owner") or info.get("creator") or "",
                    "taskType": 1,
                    "taskInfo": task_info,
                    "linkID": sid,
                    "needPassword": bool(password),
                }
            },
        )
        data = result.get("data") or {}
        task_id = (
            data.get("taskID")
            or data.get("taskId")
            or (data.get("createBatchOprTaskRes") or {}).get("taskID")
        )
        if not task_id:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "caiyun transfer missing task id",
                retryable=True,
            )

        # 轮询转存任务
        import asyncio

        for _ in range(20):
            detail = await self._request_share_api(
                account,
                TASK_PATH,
                {
                    "queryBatchOprTaskDetailReq": {
                        "taskID": task_id,
                        "msisdn": phone,
                    }
                },
            )
            task = (
                (detail.get("data") or {}).get("queryBatchOprTaskDetailRes")
                or detail.get("data")
                or {}
            )
            status = task.get("batchOprTask") or task.get("taskInfo") or {}
            state = status.get("taskStatus", status.get("status"))
            if state in (2, "Succeed", "PartialSucceed", "Failed"):
                failed = state == "Failed" or str(
                    status.get("taskResultCode", "1")
                ) in ("2", "3")
                if failed:
                    raise DomainError(
                        ErrorCode.INTERNAL_ERROR, "caiyun transfer task failed"
                    )
                break
            await asyncio.sleep(1)

        return TransferResult(remote_task_id=str(task_id))

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        result = await self._request_signed_api(
            account,
            PERSONAL_API_BASE + "/file/list",
            {
                "pageInfo": {"pageSize": 100, "pageCursor": None},
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "parentFileId": parent_id or "/",
                "imageThumbnailStyleList": ["Small", "Large"],
            },
        )
        data = result.get("data") or {}
        items = data.get("items") or []
        return [self._map_file(it) for it in items if isinstance(it, dict)]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        headers = self._auth_headers(account)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://yun.139.com/api/file/download",
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
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"caiyun download returned {resp.status_code}",
                retryable=True,
            )
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
