"""123 云盘 Adapter。

基于 123 云盘 web API 的匿名分享解析协议，自行实现轻量 HTTP 适配器
（不依赖第三方 SDK）。协议参考公开的逆向资料与 AList 的 123_share 驱动
（AGPL，仅参考协议思路，代码为本项目独立编写）。

核心协议（已公开，无需登录即可解析分享；2026-10 实测确认当前生效版）：
1. 分享短码解析：`GET https://www.123pan.cn/gsb/s/share-key?shareKey=<完整短码>`
   → `info.data.UserID`。短码形如 `A6cA-I24Jh`（shareKey 与提取码连字符拼接）。
2. 分享文件列表（匿名，新域名）：分享者个性化域名
   `GET https://<uid>.share.123pan.cn/api/share/get`
   query: shareKey, SharePwd, ParentFileId, limit, next, orderBy,
          orderDirection, Page, operateType, OrderId, superAdmin
   → data.InfoList[]（字段为大写驼峰 FileId/FileName/Type/Size）。
3. 下载直链：
   POST `/api/share/download/info`
   body: {shareKey, SharePwd, etag, fileId, s3keyFlag, size}
   返回 data.DownloadURL，其 `params` 参数为 base64 编码的真实直链。

能力边界（如实声明）：
- 「解析分享 + 文件列表 + 直链下载」无需登录（匿名）。
- 大文件（>100MB）下载可能触发风控/需登录，如实标注。
- 「转存」需要登录（账号密码 / access_token），本 Adapter 支持
  用账号凭据（access_token）转存，但需真实账号验证。
"""

from __future__ import annotations

import base64
import random
import zlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

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

# 分享短码解析域名（/gsb/s/share-key）
_SHARE_ROOT = "https://www.123pan.cn"
_GSB_SHARE_KEY = _SHARE_ROOT + "/gsb/s/share-key"
# 下载直链接口使用 yun.123pan.com 的 /b/api（与文件列表个性化域名不同）。
# 文件列表用 <uid>.share.123pan.cn/api/share/get，下载用此 BApi。
_BApi = "https://yun.123pan.com/b/api"
# 账号接口（需登录）使用 www.123pan.com/api（独立于分享 /b/api）。
_AApi = "https://www.123pan.com/api"

_HEADERS = {
    "origin": "https://yun.123pan.com",
    "referer": "https://yun.123pan.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "platform": "web",
    "app-version": "3",
}

# signPath 使用的字符映射表（数字 0-9 -> 打乱后的字符）
_SIGN_TABLE = (
    "a", "d", "e", "f", "g", "h", "l", "m", "y", "i",
    "j", "n", "o", "p", "k", "q", "r", "s", "t", "u",
    "b", "c", "v", "w", "s", "z",
)


def _sign_path(path: str, os: str = "web", version: str = "3") -> tuple[str, str]:
    """计算 123 云盘请求签名。

    返回 (key, value)，key 为 timeSign 十进制串，value 为
    `<timestamp>-<random>-<dataSign>`，作为 query 参数附加。
    """
    random_str = str(int(1e7 * random.random()))
    # 使用 CST 时区（UTC+8）
    now = datetime.now(UTC).astimezone()
    timestamp = str(int(now.timestamp()))
    now_str = now.strftime("%Y%m%d%H%M")
    mapped = "".join(_SIGN_TABLE[int(ch)] for ch in now_str)
    time_sign = str(zlib.crc32(mapped.encode("utf-8")) & 0xFFFFFFFF)
    data = "|".join([timestamp, random_str, path, os, version, time_sign])
    data_sign = str(zlib.crc32(data.encode("utf-8")) & 0xFFFFFFFF)
    return time_sign, f"{timestamp}-{random_str}-{data_sign}"


def _signed_url(raw_url: str) -> str:
    """给 URL 附加签名 query 参数。"""
    key, value = _sign_path(urlparse(raw_url).path)
    sep = "&" if "?" in raw_url else "?"
    return f"{raw_url}{sep}{key}={value}"


class Pan123Provider(ShareProvider):
    name = "123"
    display_name = "123 云盘"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "get_download_info",
            "get_share_download_info",
            "save_shared_files",
            "list_account_files",
            "validate_account",
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
                "123 account has no credential (access_token)",
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
                "123 credential could not be decrypted (secret key mismatch?)",
                retryable=False,
            ) from exc

    @staticmethod
    def _map_share_item(raw: dict[str, Any]) -> ShareFile:
        # 123 云盘分享列表字段为大写驼峰（FileId/FileName/Type/Size）。
        is_dir = int(raw.get("Type", raw.get("type", 0))) == 1
        return ShareFile(
            file_id=str(raw.get("FileId", raw.get("fileId", ""))),
            name=raw.get("FileName", raw.get("filename", "")),
            is_dir=is_dir,
            size=raw.get("Size", raw.get("size")),
            parent_id=str(
                raw.get("ParentFileId", raw.get("parentFileId", ""))
            ),
            # 下载直链所需的字段（etag/s3keyFlag），匿名下载时使用。
            raw={
                "etag": raw.get("Etag", raw.get("etag")),
                "s3keyFlag": raw.get("S3KeyFlag", raw.get("s3keyFlag")),
                "size": raw.get("Size", raw.get("size")),
                "fileId": str(raw.get("FileId", raw.get("fileId", ""))),
            },
        )

    @staticmethod
    def _map_error(data: dict[str, Any], status_code: int = 200) -> DomainError:
        code = data.get("code")
        msg = str(data.get("message", ""))
        low = msg.lower()
        # 123 云盘常见错误码
        if (
            code == 404
            or "不存在" in msg
            or "失效" in msg
            or "已删除" in msg
            or "invalid" in low
            or "not found" in low
        ):
            return DomainError(ErrorCode.INVALID_SHARE, "123 share invalid")
        if "密码" in msg or "提取码" in msg or "pwd" in low:
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "123 share password incorrect"
            )
        if code == 401 or "token" in low:
            return DomainError(
                ErrorCode.ACCOUNT_EXPIRED, "123 token invalid", retryable=False
            )
        # 5112 等：分享方流量包不足（业务限制，非协议/风控错误）。
        if code == 5112 or "流量包不足" in msg or "流量包" in msg:
            return DomainError(
                ErrorCode.QUOTA_EXCEEDED,
                "123 share quota exhausted (sharer traffic package used up)",
                retryable=False,
            )
        if "流量" in msg or "限速" in msg or "风控" in msg:
            return DomainError(
                ErrorCode.RATE_LIMITED, "123 rate limited or risk control"
            )
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"123 api error: {msg or code or status_code}",
            retryable=True,
        )

    async def _resolve_share_uid(self, share_key: str) -> str:
        """解析分享短码 → 分享者 UserID（用于构造个性化域名）。

        完整短码形如 `A6cA-I24Jh`（shareKey 与提取码连字符拼接）。
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _GSB_SHARE_KEY,
                params={"shareKey": share_key},
                headers=_HEADERS,
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"123 share-key returned {resp.status_code}",
            )
        data = resp.json()
        info = data.get("info", {})
        if info.get("code") != 0:
            raise self._map_error(info, resp.status_code)
        uid = (info.get("data") or {}).get("UserID")
        if not uid:
            raise DomainError(
                ErrorCode.INVALID_SHARE, "123 share-key missing UserID"
            )
        return str(uid)

    async def _get_share_files(
        self, share_key: str, password: str | None, parent_id: str = "0"
    ) -> list[ShareFile]:
        """匿名获取分享文件列表（分页）。

        流程：完整短码 → UserID → 个性化域名 /api/share/get。
        share_key 为完整短码（含 `-提取码`），password 为提取码（可选，
        仅当短码尾部为 1-4 位提取码时传入；否则为空）。
        """
        full_key = share_key
        if password and "-" not in share_key:
            full_key = f"{share_key}-{password}"

        uid = await self._resolve_share_uid(full_key)
        host = f"https://{uid}.share.123pan.cn"
        params = {
            "limit": "100",
            "next": "0",
            "orderBy": "file_id",
            "orderDirection": "desc",
            "shareKey": full_key,
            "SharePwd": password or "",
            "ParentFileId": parent_id,
            "Page": "1",
            "operateType": "1",
            "OrderId": "",
            "superAdmin": "false",
        }
        headers = {
            **_HEADERS,
            "origin": host,
            "referer": f"{host}/s/{full_key}",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{host}/api/share/get", params=params, headers=headers
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INVALID_SHARE,
                f"123 share list returned {resp.status_code}",
            )
        data = resp.json()
        if data.get("code") not in (0, 200):
            raise self._map_error(data, resp.status_code)
        items = data.get("data", {}).get("InfoList", [])
        return [self._map_share_item(it) for it in items]

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        files = await self._get_share_files(share_id, password)
        return Share(
            provider=self.name,
            share_id=share_id,
            title=f"123-share-{share_id}",
            files=files,
            password=password,
            raw={"share_key": share_id},
        )

    async def list_share_files(
        self, share_id: str, password: str | None, parent_id: str | None = None
    ) -> list[ShareFile]:
        return await self._get_share_files(share_id, password, parent_id or "0")

    async def validate_account(self, account: Account) -> bool:
        token = self._decrypt_credential(account)
        url = _signed_url(_AApi + "/user/info")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                headers={**_HEADERS, "authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return data.get("code") in (0, 200)

    async def save_shared_files(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        file_ids: list[str],
        destination: str | None,
        on_conflict: str,
    ) -> TransferResult:
        # 123 云盘转存需要登录账号 + 分享文件 ID，且需真实账号验证。
        # 协议存在（share 转存接口），但未验证，如实返回未实现能力，
        # 避免伪造成功。实际转存接口为 /b/api/share/save 之类，需账号。
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "123 share saving requires a logged-in account (not yet verified)",
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        token = self._decrypt_credential(account)
        params = {
            "driveId": "0",
            "limit": "100",
            "next": "0",
            "orderBy": "file_id",
            "orderDirection": "desc",
            "parentFileId": parent_id or "0",
            "trashed": "false",
            "Page": "1",
        }
        url = _signed_url(_AApi + "/file/list/new")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params=params,
                headers={**_HEADERS, "authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"123 list returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        if data.get("code") not in (0, 200):
            raise self._map_error(data)
        items = data.get("data", {}).get("InfoList", [])
        return [self._map_share_item(it) for it in items]

    async def _find_file_recursive(
        self, full_key: str, password: str | None, file_id: str,
        parent_id: str = "0", depth: int = 0,
    ) -> ShareFile | None:
        """递归查找分享中的目标文件（含子目录），返回带 raw 的 ShareFile。

        分享文件可能位于多层子目录中，直链解析需要其 etag/s3keyFlag，
        故需递归遍历定位。限制最大深度，避免异常分享导致失控递归。
        """
        if depth > 10:
            return None
        files = await self._get_share_files(full_key, password, parent_id)
        for f in files:
            if f.file_id == str(file_id):
                return f
        for f in files:
            if f.is_dir:
                found = await self._find_file_recursive(
                    full_key, password, file_id, f.file_id, depth + 1
                )
                if found is not None:
                    return found
        return None

    async def get_share_download_info(
        self,
        share_id: str,
        password: str | None,
        file_id: str,
        file: ShareFile | None = None,
    ) -> dict[str, Any]:
        """解析分享中单个文件的直链（匿名，无需登录）。

        优先复用 `file.raw`（inspect/目录遍历时已解析出的 etag/s3keyFlag/
        size/fileId）直接解析直链，避免重复递归扫描整棵分享目录。

        仅当 `file` 缺失时才回退到递归查找（兼容旧调用方）。
        """
        full_key = share_id
        if password and "-" not in share_id:
            full_key = f"{share_id}-{password}"

        target = file
        if target is None:
            target = await self._find_file_recursive(
                full_key, password, str(file_id)
            )
        if target is None:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"123 share file not found: {file_id}",
                retryable=False,
            )
        if target.is_dir:
            raise DomainError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "123 share directory download is not supported",
            )

        url = await self.resolve_share_download_url(
            full_key, password, target.raw
        )
        return {
            "url": url,
            "name": target.name,
            "size": target.size,
            "checksum": None,
            "headers": {"referer": "https://www.123pan.cn/"},
        }

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        """获取账号网盘文件的下载直链（需登录）。

        与匿名分享下载（get_share_download_info）分离：此处 file_id
        为账号网盘中的真实 fid，需 access_token。
        """
        token = self._decrypt_credential(account)
        body = {"driveId": 0, "etag": "", "fileId": int(file_id), "s3keyFlag": ""}
        url = _signed_url(_AApi + "/file/download_info")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=body,
                headers={**_HEADERS, "authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"123 download info returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        if data.get("code") not in (0, 200):
            raise self._map_error(data)
        download_url = (data.get("data") or {}).get("DownloadURL", "")
        if not download_url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "123 returned empty account download url",
                retryable=True,
            )
        return {
            "url": download_url,
            "name": None,
            "size": None,
            "checksum": None,
            "headers": {"authorization": f"Bearer {token}"},
        }

    async def resolve_share_download_url(
        self, share_key: str, password: str | None, file_info: dict[str, Any]
    ) -> str:
        """解析分享文件直链（匿名）。

        传入文件信息（etag/fileId/s3keyFlag/size），返回真实直链 URL。
        这是 123 云盘匿名下载的核心能力。
        """
        # fileId 需为整数（协议要求），从 raw 中恢复。
        try:
            fid = int(file_info.get("fileId", file_info.get("file_id", 0)))
        except (TypeError, ValueError):
            fid = file_info.get("fileId", "")
        body = {
            "shareKey": share_key,
            "SharePwd": password or "",
            "etag": file_info.get("etag"),
            "fileId": fid,
            "s3keyFlag": file_info.get("s3keyFlag"),
            "size": file_info.get("size"),
        }
        url = _signed_url(_BApi + "/share/download/info")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=_HEADERS)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                f"123 download info returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        if data.get("code") not in (0, 200):
            raise self._map_error(data)
        download_url = data.get("data", {}).get("DownloadURL", "")
        if not download_url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "123 returned empty download url",
                retryable=True,
            )
        # DownloadURL 可能含 base64 编码的 params 参数，解码得到真实直链
        parsed = urlparse(download_url)
        params = parse_qs(parsed.query).get("params")
        if params:
            try:
                decoded = base64.b64decode(params[0]).decode("utf-8")
                return decoded
            except Exception:  # noqa: BLE001
                pass
        return download_url
