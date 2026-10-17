"""腾讯微云 Adapter。

微云（share.weiyun.com）分享解析支持匿名（NoLogin 路径），转存与下载需要
登录态（`skey` cookie）。协议参考公开的微云 web API 端点（多个开源实现，
包括 Apache-2.0 的 weiyun-sdk-go 与 GPL 的 QuickSDK——本项目仅参考公开的
API 端点与请求结构，不复制任何代码）。

关键协议（body 为双层 JSON 字符串嵌套，`req_header`/`req_body`）：
1. 解析分享（匿名可选）：
   POST https://share.weiyun.com/webapp/json/weiyunShare{NoLogin}/WeiyunShareView
   body.req_body 内嵌 ".weiyun.WeiyunShareViewMsgReq_body"：{share_key, share_pwd}
2. 分享目录列表（需登录 skey）：
   POST .../weiyunShare/WeiyunShareDirList → file_list / dir_list
3. 分享下载（需登录）：
   POST .../weiyunShare/WeiyunShareBatchDownload → https_download_url
4. 分享转存（需登录）：
   POST .../weiyunShare/WeiyunSharePartSaveData → 保存到账号网盘

`g_tk`：匿名用固定值 `854045919`；登录后取自 `wyctoken` cookie。
`skey`：登录态取自 `weiyun.com` 域的 `skey` cookie。

能力边界（如实声明）：
- 「解析分享」匿名可用（NoLogin 路径）。
- 「转存 / 下载 / 账号文件列表」需登录（skey cookie），协议已实现但未经
  真实账号验证。
"""

from __future__ import annotations

import json
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

_BASE = "https://share.weiyun.com"
_WWW_BASE = "https://www.weiyun.com"
_ANON_GTK = "854045919"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://share.weiyun.com/",
}


def build_body(cmd: int, inner: str, skey: str | None = None) -> dict[str, str]:
    """构造微云请求体（req_header + req_body 双层 JSON 字符串嵌套）。

    协议要求 body 的 req_header/req_body 均为 JSON 字符串（而非嵌套对象）。
    `inner` 形如 `".weiyun.XXX_body":{...}`，通过字符串拼接进 ReqMsg_body，
    避免对键名做额外转义。
    """
    req_header = json.dumps(
        {
            "seq": 15677596144092318,
            "type": 1,
            "cmd": cmd,
            "appid": 30013,
            "version": 3,
            "major_version": 3,
            "minor_version": 3,
            "fix_version": 3,
            "wx_openid": "",
            "user_flag": 0,
        },
        separators=(",", ":"),
    )
    ext_req_head = {
        "token_info": {
            "token_type": 0,
            "login_key_type": 1,
            "login_key_value": skey or "",
        },
        "language_info": {"language_type": 2052},
    }
    # 用 JSON 对象构造 ReqMsg_body，再注入 inner 键值对。
    req_msg = {"ext_req_head": ext_req_head}
    # inner 是 `"key":{...}` 片段，解析为 (key, value)
    key, _, value = inner.partition(":")
    req_msg[json.loads(key)] = json.loads(value)
    req_body = json.dumps({"ReqMsg_body": req_msg}, separators=(",", ":"))
    return {"req_header": req_header, "req_body": req_body}


class WeiyunProvider(ShareProvider):
    name = "weiyun"
    display_name = "腾讯微云"
    capabilities = frozenset(
        {
            "inspect_share",
            "list_share_files",
            "save_shared_files",
            "list_account_files",
            "validate_account",
        }
    )

    def __init__(self, cipher: CredentialCipher | None = None) -> None:
        self._cipher = cipher

    # ---- 内部工具 ----

    @staticmethod
    def _parse_share_id(share_id: str) -> str:
        # share_id 形如完整 URL 或纯 key
        if "/" in share_id:
            m = re.search(r"share\.weiyun\.com/([A-Za-z0-9]+)", share_id)
            if m:
                return m.group(1)
            # 可能是 /xxx 形式
            return share_id.rstrip("/").split("/")[-1]
        return share_id

    def _cookie(self, account: Account | None) -> str:
        if account is None or not account.encrypted_credential:
            return ""
        if self._cipher is None:
            return ""
        try:
            return self._cipher.decrypt(account.encrypted_credential)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _skey_from_cookie(cookie: str) -> str | None:
        m = re.search(r"(?:^|;\s*)skey=([^;]+)", cookie)
        return m.group(1) if m else None

    @staticmethod
    def _gtk_from_cookie(cookie: str) -> str | None:
        m = re.search(r"(?:^|;\s*)wyctoken=([^;]+)", cookie)
        return m.group(1) if m else None

    async def _post(
        self,
        url: str,
        body: dict[str, str],
        cookie: str | None = None,
    ) -> dict[str, Any]:
        headers = dict(_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"weiyun api returned {resp.status_code}",
                retryable=True,
            )
        return resp.json()

    @staticmethod
    def _map_error(data: dict[str, Any], ctx: str) -> DomainError:
        ret = data.get("retcode") or data.get("ret") or data.get("code")
        msg = str(data.get("msg") or data.get("retmsg") or "")
        if ret == -30018 or "密码" in msg or "pwd" in msg.lower():
            return DomainError(
                ErrorCode.INVALID_PASSWORD, "weiyun share password incorrect"
            )
        if ret in (-2022, -30017, -110001) or "失效" in msg or "不存在" in msg:
            return DomainError(ErrorCode.INVALID_SHARE, "weiyun share invalid")
        return DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"weiyun {ctx}: {msg or ret}",
            retryable=True,
        )

    @staticmethod
    def _map_share_file(raw: dict[str, Any]) -> ShareFile:
        is_dir = bool(raw.get("dir_key")) and not raw.get("file_id")
        return ShareFile(
            file_id=str(
                raw.get("file_id") or raw.get("dir_key") or raw.get("file_key") or ""
            ),
            name=raw.get("filename")
            or raw.get("dir_name")
            or raw.get("file_name")
            or "",
            is_dir=is_dir,
            size=raw.get("file_size"),
            parent_id=str(raw.get("pdir_key") or ""),
        )

    # ---- Provider 接口 ----

    async def inspect_share(self, share_id: str, password: str | None) -> Share:
        """匿名解析分享（NoLogin 路径）。"""
        key = self._parse_share_id(share_id)
        inner = (
            f'".weiyun.WeiyunShareViewMsgReq_body":'
            f'{{"share_key":"{key}","share_pwd":{json.dumps(password or "")}}}'
        )
        body = build_body(12002, inner)
        data = await self._post(
            _BASE + f"/webapp/json/weiyunShareNoLogin/WeiyunShareView"
            f"?refer=chrome_windows&g_tk={_ANON_GTK}&r=0.5",
            body,
        )
        rsp = (data.get("data") or {}).get("rsp_body") or {}
        if not rsp.get("RspMsg_body"):
            raise self._map_error(data, "share view")
        rsp_body = rsp["RspMsg_body"]
        files = self._extract_files(rsp_body)
        return Share(
            provider=self.name,
            share_id=key,
            title=rsp_body.get("share_name") or f"weiyun-{key}",
            files=files,
            password=password,
            raw=rsp_body,
        )

    @staticmethod
    def _extract_files(rsp_body: dict[str, Any]) -> list[ShareFile]:
        files: list[ShareFile] = []
        for f in rsp_body.get("file_list") or []:
            files.append(WeiyunProvider._map_share_file(f))
        for d in rsp_body.get("dir_list") or []:
            files.append(WeiyunProvider._map_share_file(d))
        return files

    async def list_share_files(
        self,
        share_id: str,
        password: str | None,
        parent_id: str | None = None,
        account: Account | None = None,
    ) -> list[ShareFile]:
        # 匿名解析即可拿到顶层文件列表；子目录列表需登录 skey。
        share = await self.inspect_share(share_id, password)
        return share.files

    async def validate_account(self, account: Account) -> bool:
        cookie = self._cookie(account)
        if not self._skey_from_cookie(cookie):
            return False
        inner = (
            '".weiyun.DiskUserInfoGetMsgReq_body":'
            '{"is_get_upload_flow_flag":true,"is_get_high_speed_flow_info":true,'
            '"is_get_weiyun_flag":true}'
        )
        gtk = self._gtk_from_cookie(cookie) or _ANON_GTK
        body = build_body(2201, inner, self._skey_from_cookie(cookie))
        try:
            data = await self._post(
                _WWW_BASE + f"/webapp/json/weiyunQdiskClient/DiskUserInfoGet"
                f"?refer=chrome_windows&g_tk={gtk}&r=0.5",
                body,
                cookie=cookie,
            )
            return bool((data.get("data") or {}).get("rsp_body"))
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
        cookie = self._cookie(account)
        skey = self._skey_from_cookie(cookie)
        if not skey:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "weiyun save requires login (skey cookie)",
            )
        key = self._parse_share_id(share_id)
        share = await self.inspect_share(key, password)
        selected = [f for f in share.files if f.file_id in set(file_ids)] or share.files
        # 构造转存 file_list：pdir_key/file_id/filename/file_size
        entries = []
        for f in selected:
            entries.append(
                {
                    "pdir_key": f.parent_id or "",
                    "file_id": f.file_id,
                    "filename": f.name,
                    "file_size": f.size or 0,
                }
            )
        inner = (
            f'".weiyun.WeiyunSharePartSaveDataMsgReq_body":'
            f'{{"os_info":"windows","browser":"chrome","share_key":"{key}",'
            f'"pwd":{json.dumps(password or "")},'
            f'"file_list":{json.dumps(entries, ensure_ascii=False)},'
            f'"is_save_all":true}}'
        )
        gtk = self._gtk_from_cookie(cookie) or _ANON_GTK
        body = build_body(12025, inner, skey)
        data = await self._post(
            _BASE + f"/webapp/json/weiyunShare/WeiyunSharePartSaveData"
            f"?refer=chrome_windows&g_tk={gtk}&r=0.5",
            body,
            cookie=cookie,
        )
        rsp = (data.get("data") or {}).get("rsp_body") or {}
        if not rsp.get("RspMsg_body"):
            raise self._map_error(data, "save")
        saved = rsp["RspMsg_body"].get("saved_file_list") or rsp["RspMsg_body"].get(
            "file_list"
        ) or []
        ids = [str(f.get("file_id") or f.get("pdir_key") or "") for f in saved]
        return TransferResult(saved_file_ids=ids)

    async def list_account_files(
        self, account: Account, parent_id: str | None = None
    ) -> list[ShareFile]:
        cookie = self._cookie(account)
        skey = self._skey_from_cookie(cookie)
        if not skey:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "weiyun account listing requires login (skey cookie)",
            )
        inner = (
            '".weiyun.DiskDirBatchListMsgReq_body":{"pdir_key":'
            f'"{parent_id or ""}","dir_list":[{{"dir_key":"{parent_id or ""}",'
            '"get_type":0,"start":0,"count":100,"sort_field":2,'
            '"reverse_order":false,"get_abstract_url":true,'
            '"get_dir_detail_info":true}]}'
        )
        gtk = self._gtk_from_cookie(cookie) or _ANON_GTK
        body = build_body(2209, inner, skey)
        data = await self._post(
            _WWW_BASE + f"/webapp/json/weiyunQdisk/DiskDirBatchList"
            f"?refer=chrome_windows&g_tk={gtk}&r=0.5",
            body,
            cookie=cookie,
        )
        rsp = (data.get("data") or {}).get("rsp_body") or {}
        rsp_body = rsp.get("RspMsg_body") or {}
        files = []
        for f in rsp_body.get("file_list") or []:
            files.append(self._map_share_file(f))
        for d in rsp_body.get("dir_list") or []:
            files.append(self._map_share_file(d))
        return files

    async def get_download_info(
        self, account: Account, file_id: str
    ) -> dict[str, Any]:
        raise DomainError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "weiyun download requires share context (not supported standalone)",
        )
