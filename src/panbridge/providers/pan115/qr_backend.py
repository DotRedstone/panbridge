"""115 网盘扫码登录后端。

基于 p115client（MIT）的二维码登录协议，但**只复用其协议方法**，
不用 `login_with_qrcode`（它会阻塞轮询、终端打印 ASCII 二维码、
并可能把 cookie 写入 `~/115-cookies.txt` 默认路径）。

协议流程（端点与状态枚举自 p115client `login_with_qrcode` 抽取）：
1. `login_qrcode_token(app="web")` → `data`：含 `uid`（登录会话 id）、
   `qrcode`（二维码内容，可能是 URL 或字符串）、`time`/`sign`（轮询 payload）。
2. `login_qrcode_scan_status(qrcode_token)` → `data.status`：
   0=waiting / 1=scanned / 2=signed in / -1=expired / -2=canceled。
3. `login_qrcode_scan_result(uid, app=...)` → 返回含 cookie 的响应，
   提取 cookie 字符串作为最终凭据。

凭据为 cookie 字符串（UID/CID/SEID/KID 等），入库时加密，不写默认路径。
"""

from __future__ import annotations

import logging
from typing import Any

from panbridge.application.auth import (
    QrAuthBackend,
    QrCredential,
    QrStatus,
    QrTicket,
)
from panbridge.domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖可选
    from p115client import P115Client
    from p115client import check_response as _check_response
except ImportError:  # pragma: no cover
    P115Client = None  # type: ignore
    _check_response = None  # type: ignore


class Pan115QrBackend(QrAuthBackend):
    """115 网盘扫码登录后端（cookie 凭据）。"""

    name = "115"
    display_name = "115 网盘"

    def __init__(self, app: str = "web") -> None:
        if P115Client is None:  # pragma: no cover
            raise RuntimeError(
                "p115client 未安装，无法使用 115 扫码登录。pip install p115client"
            )
        self._app = app

    async def start(self) -> QrTicket:
        try:
            resp = await P115Client.login_qrcode_token(
                app=self._app, async_=True
            )
            data = _check_response(resp)["data"] if _check_response else resp["data"]
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.INTERNAL_ERROR,
                f"115 qr generate failed: {exc.__class__.__name__}",
                retryable=True,
            ) from exc

        qrcode = data.get("qrcode") or ""
        uid = data.get("uid") or ""
        if not qrcode:
            qrcode = f"https://115.com/scan/dg-{uid}"

        # qrcode 可能是二维码 URL 或纯字符串；若为纯字符串，构造可扫码链接。
        qr_url = qrcode if qrcode.startswith(("http://", "https://")) else (
            f"https://115.com/scan/dg-{uid}"
        )

        # state 持有轮询 payload（含 uid/time/sign）与 uid，供 check/finish。
        return QrTicket(
            qr_url=qr_url,
            state={"payload": data, "uid": uid},
        )

    async def check(self, ticket: QrTicket) -> QrStatus:
        payload = ticket.state["payload"]
        try:
            resp = await P115Client.login_qrcode_scan_status(
                payload, async_=True
            )
            data = _check_response(resp)["data"] if _check_response else resp["data"]
            status = data.get("status")
        except Exception:  # noqa: BLE001
            return QrStatus(status="waiting")

        if status == 0:
            return QrStatus(status="waiting")
        if status == 1:
            return QrStatus(status="scanned")
        if status == 2:
            return QrStatus(status="success")
        if status == -1:
            return QrStatus(status="expired")
        if status == -2:
            return QrStatus(status="cancelled")
        return QrStatus(status="failed")

    async def finish(self, ticket: QrTicket) -> QrCredential:
        uid = ticket.state["uid"]
        try:
            resp = await P115Client.login_qrcode_scan_result(
                uid, app="alipaymini", async_=True
            )
            data = _check_response(resp)["data"] if _check_response else resp["data"]
            cookie = self._extract_cookie(resp, data)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                f"115 qr login result failed: {exc.__class__.__name__}",
                retryable=False,
            ) from exc

        if not cookie:
            raise DomainError(
                ErrorCode.ACCOUNT_EXPIRED,
                "115 qr login returned empty cookie",
                retryable=False,
            )
        return QrCredential(credential=cookie, label="115-qr")

    @staticmethod
    def _extract_cookie(resp: Any, data: Any) -> str:
        """从扫码结果提取 cookie 字符串。

        115 `login/qrcode` 响应的 `data` 含 `cookie` 字段（可能是字符串
        或 `{UID, CID, SEID, KID, ...}` 字典），或顶层含 `cookies`。
        提取为 `k=v; k=v` 形式的字符串。
        """
        cookie = None
        if isinstance(data, dict):
            cookie = data.get("cookie")
            if cookie is None:
                cookie = data.get("cookies")
        if cookie is None and isinstance(resp, dict):
            cookie = resp.get("cookie") or resp.get("cookies")

        if cookie is None:
            return ""
        if isinstance(cookie, str):
            return cookie.strip().rstrip(";")
        if isinstance(cookie, dict):
            return "; ".join(
                f"{k}={v}" for k, v in cookie.items() if v
            )
        return str(cookie)
