"""迅雷云盘 Adapter。

迅雷云盘（pan.xunlei.com）使用 Android 移动端签名体系。协议参考
pan-relay（MIT 许可）的 `xunlei_client.py`，代码为本项目独立编写，
不依赖第三方 SDK。

认证：refresh_token 换取 access_token（`xluser-ssl.xunlei.com/v1/auth/token`），
每次网盘请求前用 captcha MD5 链签名换取 captcha_token。

关键协议端点：
1. 换 access_token：POST https://xluser-ssl.xunlei.com/v1/auth/token
   body={client_id, grant_type:refresh_token, refresh_token, client_secret}
2. captcha token：POST https://xluser-ssl.xunlei.com/v1/shield/captcha/init
   meta.captcha_sign = MD5 链（10 轮盐值）
3. 解析分享：GET https://api-pan.xunlei.com/drive/v1/share?share_id=&pass_code=
   → files[]（字段 id / name / kind）、pass_code_token
4. 转存：POST /drive/v1/share/restore
   body={parent_id, share_id, pass_code_token, ancestor_ids, file_ids}
   → restore_task_id
5. 任务轮询：GET /drive/v1/tasks/{task_id} → progress==100
6. 账号文件列表：GET /drive/v1/files?parent_id=&limit=
7. 下载直链：GET /drive/v1/files/{file_id} → web_content_link / medias[].link.url

能力边界（如实声明，2026-10-22 真机验证）：
- **分享解析 / 目录浏览：匿名可用**。captcha 端点（/v1/shield/captcha/init）
  无需登录即可签发 captcha_token，带 `x-captcha-token` 即可读
  `/drive/v1/share`（根目录）与 `/drive/v1/share/detail`（子目录）。
- **转存 / 账号列表 / 下载直链：需 refresh_token**。匿名请求
  `/drive/v1/files/{id}` 返回 401（转存后取直链是平台设计的唯一路径），
  故 `get_download_info` 仍为登录态能力。
"""

from __future__ import annotations

import hashlib
import time
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

_API = "https://api-pan.xunlei.com"
_AUTH_API = "https://xluser-ssl.xunlei.com"

# 匿名请求用的固定设备种子：captcha 端点无需登录即可签发 token，
# 分享解析/目录浏览可匿名完成（下载直链仍需登录态）。
_ANON_DEVICE_SEED = "panbridge-anonymous-device-v1"

_CLIENT_ID = "Xp6vsxz_7IYVw2BB"
_CLIENT_SECRET = "Xp6vsy4tN9toTVdMSpomVdXpRmES"
_CLIENT_VERSION = "8.31.0.9726"
_PACKAGE_NAME = "com.xunlei.downloadprovider"
_USER_AGENT = (
    "ANDROID-com.xunlei.downloadprovider/8.31.0.9726 netWorkType/5G appid/40 "
    "deviceName/Xiaomi_M2004j7ac deviceModel/M2004J7AC OSVersion/12 "
    "protocolVersion/301 platformVersion/10 sdkVersion/512000 Oauth2Client/0.9 "
    "(Linux 4_14_186-perf-gddfs8vbb238b) (JAVA 0)"
)

_ALGORITHMS = [
    "9uJNVj/wLmdwKrJaVj/omlQ",
    "Oz64Lp0GigmChHMf/6TNfxx7O9PyopcczMsnf",
    "Eb+L7Ce+Ej48u",
    "jKY0",
    "ASr0zCl6v8W4aidjPK5KHd1Lq3t+vBFf41dqv5+fnOd",
    "wQlozdg6r1qxh0eRmt3QgNXOvSZO6q/GXK",
    "gmirk+ciAvIgA/cxUUCema47jr/YToixTT+Q6O",
    "5IiCoM9B1/788ntB",
    "P07JH0h6qoM6TSUAK2aL9T5s2QBVeY9JWvalf",
    "+oK0AN",
]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def captcha_sign(
    client_id: str, version: str, package: str, device_id: str, ts: str
) -> str:
    """计算迅雷 captcha 签名（10 轮 MD5 链）。"""
    s = client_id + version + package + device_id + ts
    for salt in _ALGORITHMS:
        s = _md5(s + salt)
    return "1." + s


def derive_device_id(refresh_token: str) -> str:
    return _md5(f"{refresh_token}xlrefresh")


class XunleiProvider(ShareProvider):
    name = "xunlei"
    display_name = "迅雷云盘"
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
        self._access_token_cache: dict[str, dict[str, Any]] = {}

    # ---- 内部工具 ----

    def _credential(self, account: Account) -> str:
        """返回 refresh_token（明文）。"""
        if account is None or not account.encrypted_credential:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "xunlei account has no credential (refresh_token)",
            )
        if self._cipher is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "credential cipher not configured; cannot decrypt token",
            )
        try:
            return self._cipher.decrypt(account.encrypted_credential)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "xunlei credential could not be decrypted",
            ) from exc

    def _device_id(self, refresh_token: str) -> str:
        return derive_device_id(refresh_token)

    async def _get_access_token(self, account: Account) -> str:
        refresh_token = self._credential(account)
        now = time.time()
        cached = self._access_token_cache.get(refresh_token)
        if cached and now < cached.get("expires_at", 0) - 60:
            return cached["access_token"]

        device_id = self._device_id(refresh_token)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-client-id": _CLIENT_ID,
            "x-device-id": device_id,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _AUTH_API + "/v1/auth/token",
                json={
                    "client_id": _CLIENT_ID,
                    "client_secret": _CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers=headers,
            )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "xunlei refresh_token invalid",
            )
        data = resp.json()
        token_data = data.get("data") if isinstance(data.get("data"), dict) else data
        access_token = token_data.get("access_token", "")
        if not access_token:
            err = data.get("error_description") or data.get("msg") or "unknown"
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                f"xunlei auth failed: {err}",
            )
        expires_in = int(token_data.get("expires_in", 7200))
        self._access_token_cache[refresh_token] = {
            "access_token": access_token,
            "expires_at": now + expires_in,
        }
        return access_token

    async def _get_captcha_token(self, account: Account, action: str) -> str:
        refresh_token = self._credential(account)
        device_id = self._device_id(refresh_token)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._captcha_token_for(client, device_id, action)

    async def _captcha_token_for(
        self, client: httpx.AsyncClient, device_id: str, action: str
    ) -> str:
        """换取 captcha_token（该端点不需要登录态）。"""
        ts = str(int(time.time() * 1000))
        sign = captcha_sign(
            _CLIENT_ID, _CLIENT_VERSION, _PACKAGE_NAME, device_id, ts
        )
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-client-id": _CLIENT_ID,
            "x-device-id": device_id,
        }
        resp = await client.post(
            _AUTH_API + "/v1/shield/captcha/init",
            json={
                "action": action,
                "captcha_token": "",
                "client_id": _CLIENT_ID,
                "device_id": device_id,
                "redirect_uri": "xlaccsdk01://xunlei.com/callback?state=harbor",
                "meta": {
                    "client_version": _CLIENT_VERSION,
                    "package_name": _PACKAGE_NAME,
                    "user_id": "",
                    "timestamp": ts,
                    "captcha_sign": sign,
                },
            },
            headers=headers,
        )
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"xunlei captcha init returned {resp.status_code}",
                retryable=True,
            )
        data = resp.json()
        token = data.get("captcha_token") or (
            data.get("data") or {}
        ).get("captcha_token", "")
        if not token:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "xunlei captcha token empty", retryable=True
            )
        return token

    async def _anon_get(
        self, action: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """匿名（无账号）调用网盘 API。

        迅雷的 captcha 端点不需要登录即可签发 token，因此分享解析与
        目录浏览可匿名完成；涉及账号的端点会返回 401，映射为
        ACTION_REQUIRED 而非伪造成功。
        """
        device_id = derive_device_id(_ANON_DEVICE_SEED)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-client-id": _CLIENT_ID,
            "x-device-id": device_id,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            captcha = await self._captcha_token_for(client, device_id, action)
            resp = await client.get(
                _API + path,
                params=params,
                headers={**headers, "x-captcha-token": captcha},
            )
        if resp.status_code in (401, 403):
            raise DomainError(
                ErrorCode.ACTION_REQUIRED,
                "xunlei requires login for this operation",
            )
        if resp.status_code not in (200, 201):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"xunlei {path.rsplit('/', 1)[-1]} returned {resp.status_code}",
                retryable=True,
            )
        return resp.json()

    async def _request_pan(
        self,
        account: Account,
        method: str,
        url: str,
        action: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        access_token = await self._get_access_token(account)
        captcha = await self._get_captcha_token(account, action)
        refresh_token = self._credential(account)
        device_id = self._device_id(refresh_token)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-client-id": _CLIENT_ID,
            "x-device-id": device_id,
            "Authorization": f"Bearer {access_token}",
            "x-captcha-token": captcha,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, json=payload, params=params, headers=headers
            )
        if resp.status_code not in (200, 201):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"xunlei {url.split('/')[-1]} returned {resp.status_code}",
                retryable=True,
            )
        return resp.json()

    @staticmethod
    def _parse_share_id(share_id: str) -> str:
        if "/" in share_id:
            import re

            m = re.search(r"/s/([^?#/]+)", share_id)
            return m.group(1) if m else share_id
        return share_id

    @staticmethod
    def _map_file(raw: dict[str, Any]) -> ShareFile:
        return ShareFile(
            file_id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            is_dir=raw.get("kind", "") == "drive#folder",
            size=int(raw.get("size", 0)) if raw.get("size") else None,
            parent_id=str(raw.get("parent_id", "")),
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        """匿名解析分享（无需登录，2026-10 真机验证）。

        迅雷的 captcha 端点可匿名签发 token，`/drive/v1/share` 匿名可读，
        返回分享根目录文件列表与 `pass_code_token`（子目录遍历凭据）。
        下载直链仍需登录态（匿名 `/drive/v1/files/{id}` 返回 401）。
        """
        sid = self._parse_share_id(share_id)
        data = await self._anon_get(
            "GET:/drive/v1/share", "/drive/v1/share", self._share_params(sid, password)
        )
        self._raise_share_status(data)
        files = [self._map_file(f) for f in data.get("files", [])]
        title = files[0].name if files else f"xunlei-{sid}"
        return Share(
            provider=self.name,
            share_id=sid,
            title=title,
            files=files,
            password=password,
            raw={
                "pass_code_token": data.get("pass_code_token", ""),
                "file_num": data.get("file_num"),
            },
        )

    @staticmethod
    def _share_params(
        share_id: str, password: str | None, pass_code_token: str = ""
    ) -> dict[str, Any]:
        """分享接口的查询参数（与网页端保持一致）。"""
        return {
            "share_id": share_id,
            "pass_code": password or "",
            "limit": 100,
            "keyword": "",
            "pass_code_token": pass_code_token,
            "page_token": "",
            "scene": "NORMAL",
            "order": "DEFAULT_ORDER",
            "thumbnail_size": "SIZE_MEDIUM",
        }

    @staticmethod
    def _raise_share_status(data: dict[str, Any]) -> None:
        """把分享接口的 share_status 映射为稳定的领域错误。"""
        if data.get("error_code"):
            raise XunleiProvider._map_error(data, "share")
        status = data.get("share_status")
        if status in (None, "", "OK"):
            return
        if status == "PASS_CODE_ERROR":
            raise DomainError(
                ErrorCode.INVALID_PASSWORD, "xunlei pass code incorrect"
            )
        if status == "PASS_CODE_EMPTY":
            raise DomainError(
                ErrorCode.ACTION_REQUIRED, "xunlei share requires pass code"
            )
        if status == "SENSITIVE_RESOURCE":
            raise DomainError(
                ErrorCode.INVALID_SHARE, "xunlei share blocked by platform"
            )
        raise DomainError(
            ErrorCode.INVALID_SHARE,
            f"xunlei share status: {data.get('share_status_text') or status}",
        )

    async def _fetch_share(
        self,
        account: Account,
        share_id: str,
        password: str | None,
        parent_id: str | None = None,
    ) -> tuple[str, list[ShareFile], dict[str, Any]]:
        """登录态解析分享（根目录 + 可选子目录）。"""
        sid = self._parse_share_id(share_id)
        root = await self._request_pan(
            account,
            "GET",
            _API + "/drive/v1/share",
            action="GET:/drive/v1/share",
            params=self._share_params(sid, password),
        )
        self._raise_share_status(root)
        token = root.get("pass_code_token", "")
        if not parent_id:
            files = [self._map_file(f) for f in root.get("files", [])]
            return token, files, root
        detail = await self._request_pan(
            account,
            "GET",
            _API + "/drive/v1/share/detail",
            action="GET:/drive/v1/share/detail",
            params={
                **self._share_params(sid, password, token),
                "parent_id": parent_id,
            },
        )
        self._raise_share_status(detail)
        files = [self._map_file(f) for f in detail.get("files", [])]
        return token, files, detail

    @staticmethod
    def _map_error(data: dict[str, Any], ctx: str) -> DomainError:
        msg = str(
            data.get("error_description")
            or data.get("error")
            or data.get("error_code")
            or ""
        )
        low = msg.lower()
        if "pass_code" in low or "password" in low:
            return DomainError(ErrorCode.INVALID_PASSWORD, "xunlei pass code incorrect")
        if "not_found" in low or "不存在" in msg or "invalid" in low:
            return DomainError(ErrorCode.INVALID_SHARE, "xunlei share invalid")
        return DomainError(
            ErrorCode.INTERNAL_ERROR, f"xunlei {ctx}: {msg}", retryable=True
        )

    async def list_share_files(
        self,
        share_id: str,
        password: str | None,
        parent_id: str | None = None,
        account: Account | None = None,
    ) -> list[ShareFile]:
        """列出分享目录内容（匿名可用，子目录走 /drive/v1/share/detail）。"""
        if account is not None:
            _, files, _ = await self._fetch_share(
                account, share_id, password, parent_id
            )
            return files
        sid = self._parse_share_id(share_id)
        root = await self._anon_get(
            "GET:/drive/v1/share", "/drive/v1/share", self._share_params(sid, password)
        )
        self._raise_share_status(root)
        if not parent_id:
            return [self._map_file(f) for f in root.get("files", [])]
        detail = await self._anon_get(
            "GET:/drive/v1/share/detail",
            "/drive/v1/share/detail",
            {
                **self._share_params(
                    sid, password, root.get("pass_code_token", "")
                ),
                "parent_id": parent_id,
            },
        )
        self._raise_share_status(detail)
        return [self._map_file(f) for f in detail.get("files", [])]

    async def validate_account(self, account: Account) -> bool:
        try:
            token = await self._get_access_token(account)
            return bool(token)
        except DomainError:
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
        pass_token, files, _ = await self._fetch_share(account, sid, password)
        selected = [f.file_id for f in files if f.file_id in set(file_ids)]
        if not selected:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "xunlei: no matching files selected for transfer",
            )
        result = await self._request_pan(
            account,
            "POST",
            _API + "/drive/v1/share/restore",
            action="POST:/drive/v1/share/restore",
            payload={
                "parent_id": destination or "",
                "share_id": sid,
                "pass_code_token": pass_token,
                "ancestor_ids": [],
                "specify_parent_id": True,
                "file_ids": selected,
            },
        )
        if result.get("error_code"):
            raise self._map_error(result, "restore")
        task_id = result.get("restore_task_id")
        if not task_id:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                "xunlei restore missing task_id",
                retryable=True,
            )

        saved = await self._wait_task(account, task_id)
        return TransferResult(saved_file_ids=saved, remote_task_id=str(task_id))

    async def _wait_task(
        self, account: Account, task_id: str, retries: int = 30
    ) -> list[str]:
        import asyncio

        for _ in range(retries):
            result = await self._request_pan(
                account,
                "GET",
                f"{_API}/drive/v1/tasks/{task_id}",
                action="GET:/drive/v1/tasks",
            )
            if result.get("error_code"):
                continue
            if result.get("progress") == 100:
                trace = (result.get("params") or {}).get("trace_file_ids") or ""
                ids: list[str] = []
                if trace:
                    import json

                    try:
                        parsed = json.loads(trace) if isinstance(trace, str) else trace
                        if isinstance(parsed, dict):
                            ids = [str(v) for v in parsed.values() if v]
                        elif isinstance(parsed, list):
                            ids = [str(v) for v in parsed if v]
                    except (ValueError, TypeError):
                        ids = []
                if not ids and isinstance(
                    (result.get("params") or {}).get("file_ids"), list
                ):
                    ids = [str(v) for v in result["params"]["file_ids"] if v]
                return ids
            await asyncio.sleep(1)
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, "xunlei task polling timed out", retryable=True
        )

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        result = await self._request_pan(
            account,
            "GET",
            _API + "/drive/v1/files",
            action="GET:/drive/v1/files",
            params={"parent_id": parent_id or "", "limit": 100},
        )
        if result.get("error_code"):
            raise self._map_error(result, "files")
        return [self._map_file(f) for f in result.get("files", [])]

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        result = await self._request_pan(
            account,
            "GET",
            f"{_API}/drive/v1/files/{file_id}",
            action=f"GET:/drive/v1/files/{file_id}",
        )
        if result.get("error_code"):
            raise self._map_error(result, "download")
        url = result.get("web_content_link") or ""
        if not url:
            for media in result.get("medias", []) or []:
                if (media.get("link") or {}).get("url"):
                    url = media["link"]["url"]
                    break
        if not url:
            raise DomainError(
                ErrorCode.DOWNLOAD_FAILED,
                "xunlei returned empty download link",
                retryable=True,
            )
        return {
            "url": url,
            "name": result.get("name"),
            "size": int(result.get("size", 0)) if result.get("size") else None,
            "checksum": result.get("sha1"),
            "headers": {"User-Agent": _USER_AGENT},
        }
